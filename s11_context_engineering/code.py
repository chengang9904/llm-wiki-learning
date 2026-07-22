# -*- coding: utf-8 -*-
"""
s11：上下文工程 —— Skill 按需加载 + 历史压缩

上下文是 Agent 最贵、最稀缺的资源。s10 用子 Agent 隔离了「过程」，但还有两个漏洞：

  1. **方法论占地**：把「怎么分析 Go 项目 / 怎么追调用链 / 怎么修页面」全塞进
     system prompt，每轮都付这笔 token，而多数任务只用得到其中一份；
  2. **父上下文仍线性增长**：轮次多了照样逼近上限，一旦满了任务就断。

s11 各给一个机制：

  Skill 按需加载（对照 skills/ + skill_read.go）：
    - skills/*.md：frontmatter（name + 一行 description）+ 正文方法论；
    - 启动时只把「名称 + 一行说明」放进 system prompt（索引 ~几百字符）；
    - 模型判断需要时 load_skill(name) 拿全文——知识的懒加载。

  历史压缩（对照 agent/memory/consolidator.go）：
    - 每轮调用前估算上下文（chars//3 的粗估）；
    - 超过阈值：把「中段历史」交给一次 LLM 调用压成摘要（保留：原始任务、
      已确认的关键发现（带文件:行号）、未完成事项），
      重组为 [system, 摘要消息, 最近几条消息]；
    - 尾部切点必须落在消息边界的安全处（不能把 assistant 的 tool_calls
      和它的 tool 结果拆开——OpenAI API 会直接报错）；
    - 压缩后模型凭摘要里的关键发现**继续任务，不重扫**。

  工具截断的系统化（对照 tools/truncate.go）：
    - s07 起的「全局 4000 字符」升级为分工具预算表 TOOL_OUTPUT_BUDGETS——
      read_file 值得多留（正文是证据），list_files 不值得。

对照 WeKnora：
  - skills/preloaded/<name>/SKILL.md：同款 frontmatter（name+description）；
    skill_read.go 是加载工具；WeKnora 启动同样只注入技能索引；
  - agent/memory/consolidator.go：DefaultConsolidationThreshold = 0.5
    （占上下文 50% 触发），压缩目标是阈值的 60%（:110）——教学版用命令行
    阈值参数演示同一机制；
  - tools/truncate.go / sanitize_messages.go：工具返回截断与消息序列修理
    （教学版的「安全切点」就是 sanitize 思想的最小体现）。

用法：
    python code.py query "..." [--mock] [--compress-threshold N]
    （--compress-threshold 默认 24000 估算 token；demo 用 3000 强制触发压缩）
"""

import argparse
import datetime
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# ─────────────────────────── 配置 ───────────────────────────

SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", r"C:\Desktop\Project\WeKnora")).resolve()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = Path(os.environ.get("WIKI_WORKSPACE", PROJECT_ROOT / "workspace")).resolve()
WIKI_DIR = WORKSPACE / "wiki"
STATE_DIR = WORKSPACE / "state"
SKILLS_DIR = PROJECT_ROOT / "skills"

MAX_ITERATIONS_DEFAULT = 20
SUBAGENT_MAX_ITERATIONS = 8
COMPRESS_THRESHOLD_DEFAULT = 24000   # 估算 token；对照 consolidator 的 0.5*上限
COMPRESS_KEEP_TAIL = 3               # 压缩时保留的最近消息条数（再按安全切点前移）
BASH_TIMEOUT = 30
READ_DEFAULT_LIMIT = 200
READ_MAX_LIMIT = 500
GREP_MAX_RESULTS = 50
GLOB_MAX_RESULTS = 100
LIST_MAX_ENTRIES = 200
WIKI_SEARCH_MAX = 25

# 分工具截断预算（对照 tools/truncate.go）：证据类工具多留，清单类工具少留
TOOL_OUTPUT_BUDGETS = {"read_file": 8000, "read_wiki_page": 8000,
                       "spawn_subagent": 4000, "grep": 4000,
                       "load_skill": 6000, "_default": 3000}

SENSITIVE_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "credentials*", "secret*", "*.secret", "id_rsa*",
]
EXCLUDED_DIRS = {
    ".git", ".github", ".idea", ".vscode",
    "node_modules", "frontend", "web", "miniprogram",
    ".local-data", "__pycache__", ".venv", "venv",
    "vendor", "dist", "build",
}
DESTRUCTIVE_PATTERNS = ["rm -rf", "rm -r /", "mkfs", "shutdown", "reboot",
                        "git push", "git commit", "> /", "format "]
SENSITIVE_RE = re.compile(r"\.env\b|\.pem\b|\.key\b|credentials|secret|id_rsa", re.I)
PAGE_TYPE_DIRS = ["architecture", "modules", "workflows", "api", "data",
                  "infrastructure", "decisions", "glossary"]
TODO_STATUSES = ("pending", "in_progress", "completed", "blocked")
TODO_ICONS = {"pending": "○", "in_progress": "►", "completed": "✓", "blocked": "⊘"}


def load_env() -> dict:
    here = Path(__file__).resolve().parent
    for candidate in (here / ".env", here.parent / ".env"):
        if candidate.is_file():
            env = {}
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
            return env
    return {}


def load_json(path: Path, default):
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ─────────────────────────── 权限层（同 s08+） ───────────────────────────

class PermissionDenied(Exception):
    pass


def _is_within(p: Path, root: Path) -> bool:
    ps, rs = str(p).casefold(), str(root).casefold()
    return ps == rs or ps.startswith(rs.rstrip("\\/") + os.sep)


def resolve_path(raw: str, mode: str) -> Path:
    raw = raw.strip().strip('"')
    p = Path(raw)
    if not p.is_absolute():
        p = (SOURCE_ROOT / p) if mode == "read" else (PROJECT_ROOT / p)
    p = p.resolve()
    name = p.name.lower()
    if any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_PATTERNS):
        raise PermissionDenied(f"{p.name} 匹配敏感文件清单，连读都不允许（安全红线）。")
    if mode == "read":
        if not (_is_within(p, SOURCE_ROOT) or _is_within(p, PROJECT_ROOT)):
            raise PermissionDenied(f"读取被拒：{p} 不在允许的根目录内。")
    else:
        if not _is_within(p, PROJECT_ROOT):
            raise PermissionDenied(f"写入被拒：{p} 不在 {PROJECT_ROOT} 内。")
    return p


# ─────────────────────────── 工具注册表 ───────────────────────────

TOOL_REGISTRY = {}


def tool(name: str, description: str, parameters: dict):
    def decorator(fn):
        TOOL_REGISTRY[name] = {
            "schema": {"type": "function",
                       "function": {"name": name, "description": description,
                                    "parameters": parameters}},
            "handler": fn,
        }
        return fn
    return decorator


def execute_tool(name: str, args: dict, allowed: set) -> str:
    if name not in allowed:
        return f"[拒绝] 工具 {name} 在当前上下文不可用。"
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        return f"[错误] 未注册的工具：{name}"
    try:
        result = entry["handler"](**args)
    except PermissionDenied as e:
        return f"[拒绝] {e}"
    except TypeError as e:
        return f"[错误] 参数不合法：{e}"
    except Exception as e:
        return f"[错误] {type(e).__name__}: {e}"
    budget = TOOL_OUTPUT_BUDGETS.get(name, TOOL_OUTPUT_BUDGETS["_default"])
    if len(result) > budget:
        result = result[:budget] + f"\n...[截断至 {budget} 字符，原始 {len(result)}。" \
                                   "需要更多请缩小范围再调用]"
    return result


# ─────────────────────────── 源码 / Wiki / Todo 工具（同 s10） ───────────────────────────

@tool("bash", "在 WeKnora 根目录执行 bash 命令。逃生舱：优先用原子工具。",
      {"type": "object", "properties": {"command": {"type": "string"}},
       "required": ["command"]})
def tool_bash(command: str) -> str:
    lowered = command.lower()
    if SENSITIVE_RE.search(lowered):
        return "[拒绝] 命令涉及敏感文件，安全红线禁止访问。"
    for pat in DESTRUCTIVE_PATTERNS:
        if pat in lowered:
            return f"[拒绝] 命令包含黑名单模式「{pat}」。"
    bash_exe = _find_bash()
    if not bash_exe:
        return "[错误] 找不到 bash（需要 Git for Windows）。"
    try:
        result = subprocess.run([bash_exe, "-c", command], cwd=SOURCE_ROOT,
                                capture_output=True, text=True, timeout=BASH_TIMEOUT,
                                encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return f"[错误] 命令超时（>{BASH_TIMEOUT}s）。"
    return (result.stdout + result.stderr).strip() or "（无输出）"


def _find_bash() -> str:
    import shutil
    git = shutil.which("git")
    candidates = []
    if git:
        git_root = Path(git).parent.parent
        candidates += [git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    which = shutil.which("bash")
    return which if which and "system32" not in which.lower() else ""


@tool("list_files", "列出目录内容（一层）。相对路径相对 WeKnora 根。",
      {"type": "object", "properties": {"path": {"type": "string"}}, "required": []})
def tool_list_files(path: str = ".") -> str:
    p = resolve_path(path, "read")
    if not p.is_dir():
        return f"[错误] 不是目录：{p}"
    entries = []
    for child in sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
        if child.name in EXCLUDED_DIRS:
            continue
        if child.is_file() and any(fnmatch.fnmatch(child.name.lower(), pat)
                                   for pat in SENSITIVE_PATTERNS):
            continue
        entries.append(child.name + "/" if child.is_dir()
                       else f"{child.name}  ({child.stat().st_size:,} B)")
        if len(entries) >= LIST_MAX_ENTRIES:
            entries.append(f"...[已达 {LIST_MAX_ENTRIES} 条上限]")
            break
    return "\n".join(entries) or "（空目录）"


@tool("read_file", "读文件，带行号。默认前 200 行；大文件用 offset/limit 分段。",
      {"type": "object",
       "properties": {"path": {"type": "string"},
                      "offset": {"type": "integer"}, "limit": {"type": "integer"}},
       "required": ["path"]})
def tool_read_file(path: str, offset: int = 1, limit: int = READ_DEFAULT_LIMIT) -> str:
    p = resolve_path(path, "read")
    if not p.is_file():
        return f"[错误] 文件不存在：{p}"
    limit = max(1, min(limit, READ_MAX_LIMIT))
    offset = max(1, offset)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    chunk = lines[offset - 1: offset - 1 + limit]
    if not chunk:
        return f"[错误] offset={offset} 超出文件行数（共 {total} 行）"
    width = len(str(offset + len(chunk) - 1))
    numbered = "\n".join(f"{offset + i:>{width}}| {ln}" for i, ln in enumerate(chunk))
    tail = f"\n...[文件共 {total} 行，本次显示 {offset}-{offset + len(chunk) - 1}]" \
        if total > offset - 1 + len(chunk) else ""
    return numbered + tail


@tool("grep", "在目录树里正则搜索文件内容。相对路径相对 WeKnora 根。",
      {"type": "object",
       "properties": {"pattern": {"type": "string"}, "path": {"type": "string"},
                      "include": {"type": "string"}},
       "required": ["pattern"]})
def tool_grep(pattern: str, path: str = ".", include: str = "") -> str:
    root = resolve_path(path, "read")
    if not root.is_dir():
        return f"[错误] 不是目录：{root}"
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"[错误] 正则不合法：{e}"
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for fname in sorted(filenames):
            if include and not fnmatch.fnmatch(fname, include):
                continue
            if any(fnmatch.fnmatch(fname.lower(), pat) for pat in SENSITIVE_PATTERNS):
                continue
            fpath = Path(dirpath) / fname
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = fpath.relative_to(root).as_posix()
                    hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(hits) >= GREP_MAX_RESULTS:
                        hits.append(f"...[已达 {GREP_MAX_RESULTS} 条上限]")
                        return "\n".join(hits)
    return "\n".join(hits) or "（无匹配）"


@tool("glob", "按通配符找文件。相对路径相对 WeKnora 根。",
      {"type": "object",
       "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
       "required": ["pattern"]})
def tool_glob(pattern: str, path: str = ".") -> str:
    root = resolve_path(path, "read")
    if not root.is_dir():
        return f"[错误] 不是目录：{root}"
    results = []
    for p in sorted(root.glob(pattern)):
        rel_parts = p.relative_to(root).parts
        if set(rel_parts) & EXCLUDED_DIRS:
            continue
        if p.is_file() and any(fnmatch.fnmatch(p.name.lower(), pat)
                               for pat in SENSITIVE_PATTERNS):
            continue
        results.append(p.relative_to(root).as_posix())
        if len(results) >= GLOB_MAX_RESULTS:
            results.append(f"...[已达 {GLOB_MAX_RESULTS} 条上限]")
            break
    return "\n".join(results) or "（无匹配）"


def _pages_meta() -> dict:
    return load_json(STATE_DIR / "pages.json", {"pages": {}, "redirects": {}})


def _find_page_file(slug: str):
    for sub in PAGE_TYPE_DIRS:
        p = WIKI_DIR / sub / f"{slug}.md"
        if p.is_file():
            return p
    return None


@tool("list_wiki_pages", "列出 Wiki 全部页面（按类型分组）。",
      {"type": "object", "properties": {}, "required": []})
def tool_list_wiki_pages() -> str:
    meta = _pages_meta()["pages"]
    lines = []
    for sub in PAGE_TYPE_DIRS:
        d = WIKI_DIR / sub
        if not d.is_dir():
            continue
        pages = sorted(d.glob("*.md"))
        if not pages:
            continue
        lines.append(f"[{sub}]")
        for p in pages:
            slug = p.stem
            m = meta.get(slug, {})
            alias = f"，别名: {', '.join(m['aliases'])}" if m.get("aliases") else ""
            lines.append(f"  {slug} — {m.get('title', slug)}"
                         f"（来源 {len(m.get('sources', []))} 个{alias}）")
    return "\n".join(lines) or "（Wiki 为空）"


@tool("read_wiki_page", "读取 Wiki 页面全文，自动跟随合并重定向。",
      {"type": "object", "properties": {"slug": {"type": "string"}},
       "required": ["slug"]})
def tool_read_wiki_page(slug: str) -> str:
    slug = slug.strip().lower()
    if slug == "index":
        idx = WIKI_DIR / "index.md"
        return idx.read_text(encoding="utf-8") if idx.is_file() else "[错误] index.md 不存在"
    prefix = ""
    redirects = _pages_meta().get("redirects", {})
    if slug in redirects:
        target = redirects[slug]
        prefix = f"[重定向] {slug} 已被合并进 {target}：\n\n"
        slug = target
    p = _find_page_file(slug)
    if p is None:
        return f"[错误] 页面不存在：{slug}"
    return prefix + p.read_text(encoding="utf-8")


@tool("search_wiki", "在 Wiki 的标题/别名/正文里做文本搜索。",
      {"type": "object", "properties": {"query": {"type": "string"}},
       "required": ["query"]})
def tool_search_wiki(query: str) -> str:
    q = query.strip().casefold()
    if not q:
        return "[错误] query 不能为空"
    meta = _pages_meta()["pages"]
    results = []
    for sub in PAGE_TYPE_DIRS:
        d = WIKI_DIR / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            slug = p.stem
            m = meta.get(slug, {})
            title = m.get("title", slug)
            hit_title = q in title.casefold() or q in slug.casefold()
            hit_alias = any(q in a.casefold() for a in m.get("aliases", []))
            body_hits = []
            for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if q in line.casefold():
                    body_hits.append(f"    L{lineno}: {line.strip()[:150]}")
                if len(body_hits) >= 3:
                    break
            if hit_title or hit_alias or body_hits:
                tag = "标题" if hit_title else ("别名" if hit_alias else "正文")
                results.append(f"{slug} — {title}（命中: {tag}）")
                results.extend(body_hits[:3])
            if len(results) >= WIKI_SEARCH_MAX:
                results.append("...[结果过多]")
                return "\n".join(results)
    return "\n".join(results) or f"（Wiki 中没有「{query}」的匹配）"


TODOS = []


def _persist_todos():
    save_json(STATE_DIR / "session.json", {
        "todos": TODOS,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })


def _render_todos() -> str:
    if not TODOS:
        return "（计划为空）"
    return "\n".join(
        f"{t['id']}. {TODO_ICONS[t['status']]} [{t['status']}] {t['content']}"
        + (f" —— {t['note']}" if t.get("note") else "") for t in TODOS)


@tool("todo_write", "制定/重建任务计划（覆盖整个列表）。纪律：同时只一个 in_progress。",
      {"type": "object",
       "properties": {"items": {"type": "array", "items": {"type": "string"}}},
       "required": ["items"]})
def tool_todo_write(items: list) -> str:
    global TODOS
    if not items or not all(isinstance(i, str) and i.strip() for i in items):
        return "[错误] items 必须是非空字符串列表"
    TODOS = [{"id": i + 1, "content": item.strip(), "status": "pending", "note": ""}
             for i, item in enumerate(items)]
    _persist_todos()
    return "计划已建立：\n" + _render_todos()


@tool("todo_read", "查看当前任务计划。",
      {"type": "object", "properties": {}, "required": []})
def tool_todo_read() -> str:
    return _render_todos()


@tool("todo_update", "更新任务状态（pending/in_progress/completed/blocked）。",
      {"type": "object",
       "properties": {"id": {"type": "integer"},
                      "status": {"type": "string", "enum": list(TODO_STATUSES)},
                      "note": {"type": "string"}},
       "required": ["id", "status"]})
def tool_todo_update(id: int, status: str, note: str = "") -> str:
    if status not in TODO_STATUSES:
        return f"[错误] status 必须是 {'/'.join(TODO_STATUSES)}"
    for t in TODOS:
        if t["id"] == id:
            t["status"] = status
            if note:
                t["note"] = note
            _persist_todos()
            return "已更新：\n" + _render_todos()
    return f"[错误] 没有 id={id} 的任务"


# ─────────────────────────── 新增：Skill 按需加载 ───────────────────────────

def load_skills_index() -> list:
    """启动时只解析 frontmatter 的 name + description——索引轻，正文懒加载。"""
    skills = []
    if not SKILLS_DIR.is_dir():
        return skills
    for f in sorted(SKILLS_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        name = desc = ""
        if text.startswith("---"):
            for line in text.split("---", 2)[1].splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
        skills.append({"name": name or f.stem, "description": desc, "file": f})
    return skills


SKILLS_INDEX = load_skills_index()


@tool("load_skill",
      "加载一个技能的完整方法论（system prompt 里只有名称和一行说明）。"
      "开始一类新任务前，先加载对应技能，按它的步骤执行，不要凭空猜方法论。",
      {"type": "object",
       "properties": {"name": {"type": "string", "description": "技能名，见 system prompt 的技能索引"}},
       "required": ["name"]})
def tool_load_skill(name: str) -> str:
    name = name.strip().lower()
    for s in SKILLS_INDEX:
        if s["name"].lower() == name or s["file"].stem.lower() == name:
            text = s["file"].read_text(encoding="utf-8")
            body = text.split("---", 2)[2].strip() if text.startswith("---") else text
            return f"[技能 {s['name']} 已加载]\n\n{body}"
    available = ", ".join(s["name"] for s in SKILLS_INDEX)
    return f"[错误] 技能不存在：{name}。可用：{available}"


# ─────────────────────────── 新增：历史压缩 ───────────────────────────

def estimate_tokens(messages: list) -> int:
    """粗估：字符数 // 3（中英混合 + 代码的经验值）。生产版用真实 tokenizer。"""
    total = 0
    for m in messages:
        total += len(str(m.get("content", "")))
        for tc in m.get("tool_calls", []) or []:
            total += len(str(tc))
    return total // 3


def _safe_tail_start(messages: list, keep: int) -> int:
    """尾部切点：不能把 assistant(tool_calls) 与其 tool 结果拆开——
    切点处的消息不能是 role:tool（它的发起者会被压掉，API 直接报错）。
    对照 sanitize_messages.go：消息序列必须保持配对完整。"""
    start = max(2, len(messages) - keep)          # 至少保住 system + 首条 user 之后
    while start < len(messages) and messages[start]["role"] == "tool":
        start += 1
    return start


COMPRESS_PROMPT = """【压缩任务】把下面的 Agent 对话历史压缩成一份工作备忘录，用于替换原始历史。
必须保留（丢了任务就断了）：
1. 原始任务是什么；
2. 已确认的关键发现——每条带 文件路径:行号，这是接下来不必重扫的资本；
3. 未完成事项与下一步计划（含 todo 状态）。
不要保留：工具的原始输出全文、试错过程、寒暄。
直接输出备忘录正文（无需解释）。

<history>
{history}
</history>"""


def compress_history(messages: list, llm, question: str, threshold: int) -> list:
    """[system, user(任务), ...中段..., 尾部] → [system, user(摘要+任务), 尾部]"""
    tail_start = _safe_tail_start(messages, COMPRESS_KEEP_TAIL)
    middle = messages[1:tail_start]
    if len(middle) < 4:
        return messages                     # 没什么可压的
    # 序列化中段（每条限长，压缩调用本身也不能爆）
    serialized = []
    for m in middle:
        content = str(m.get("content", ""))[:1200]
        calls = "".join(f" [调用 {tc['function']['name']}]"
                        for tc in m.get("tool_calls", []) or [])
        serialized.append(f"({m['role']}){calls} {content}")
    req = [{"role": "system", "content": "你是对话压缩器，只输出备忘录正文。"},
           {"role": "user", "content": COMPRESS_PROMPT.format(
               history="\n".join(serialized))}]
    msg = llm.chat(req, [])
    summary = (msg.content or "").strip() or "（压缩失败，摘要为空）"
    before = estimate_tokens(messages)
    new_messages = [
        messages[0],
        {"role": "user", "content":
            f"原始任务：{question}\n\n【以下是被压缩的工作历史备忘录，"
            f"其中的关键发现已核实过，继续任务时直接使用，不要重扫】\n\n{summary}"},
        *messages[tail_start:],
    ]
    after = estimate_tokens(new_messages)
    print(f"    [压缩] ~{before} tok > 阈值 {threshold} → 压缩后 ~{after} tok"
          f"（{len(messages)} 条 → {len(new_messages)} 条，保留尾部 "
          f"{len(messages) - tail_start} 条）")
    return new_messages


# ─────────────────────────── 子 Agent（同 s10） ───────────────────────────

SUBAGENT_LLM_FACTORY = None
SUBAGENT_TOOLS = {"bash", "list_files", "read_file", "grep", "glob",
                  "list_wiki_pages", "read_wiki_page", "search_wiki", "load_skill"}

SUBAGENT_SYSTEM_PROMPT = f"""你是一个专注的代码调查子 Agent。被分析项目（只读）：{SOURCE_ROOT}
只做交给你的任务；结论带 文件:行号；最终回答只输出 JSON：
{{"summary": "...", "important_files": [...], "call_chains": [...], "uncertainties": [...]}}
安全红线：敏感文件连读都不允许；项目只读。"""


def _validate_subagent_result(data) -> bool:
    return (isinstance(data, dict)
            and isinstance(data.get("summary"), str) and data["summary"].strip()
            and all(isinstance(data.get(k), list)
                    for k in ("important_files", "call_chains", "uncertainties")))


def run_subagent(task: str, context: str) -> dict:
    llm = SUBAGENT_LLM_FACTORY(task)
    user = task if not context else f"{task}\n\n补充背景：{context}"
    messages = [{"role": "system", "content": SUBAGENT_SYSTEM_PROMPT},
                {"role": "user", "content": user}]
    tools = [TOOL_REGISTRY[n]["schema"] for n in TOOL_REGISTRY if n in SUBAGENT_TOOLS]
    final = ""
    for round_no in range(1, SUBAGENT_MAX_ITERATIONS + 1):
        msg = llm.chat(messages, tools)
        if msg.content:
            print(f"    │ [子Agent 第{round_no}轮] {msg.content[:100]}")
        messages.append({
            "role": "assistant", "content": msg.content or "",
            **({"tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls]} if msg.tool_calls else {}),
        })
        if not msg.tool_calls:
            final = msg.content or ""
            break
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            print(f"    │ [子工具] {tc.function.name}"
                  f"({json.dumps(args, ensure_ascii=False)[:100]})")
            result = execute_tool(tc.function.name, args, SUBAGENT_TOOLS)
            print(f"    │ [输出] {result[:150].replace(chr(10), ' ⏎ ')}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    text = final.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if _validate_subagent_result(data):
                return data
        except json.JSONDecodeError:
            pass
    return {"summary": final or "（子 Agent 未返回内容）",
            "important_files": [], "call_chains": [],
            "uncertainties": ["子 Agent 未按 JSON 结构返回，原文置于 summary"]}


@tool("spawn_subagent",
      "派生独立上下文的子 Agent 执行单个调查子任务。任务描述必须自包含。",
      {"type": "object",
       "properties": {"task": {"type": "string"}, "context": {"type": "string"}},
       "required": ["task"]})
def tool_spawn_subagent(task: str, context: str = "") -> str:
    print(f"    ┌─ 子 Agent 启动：{task[:80]}")
    result = run_subagent(task, context)
    print(f"    └─ 子 Agent 返回")
    return json.dumps(result, ensure_ascii=False, indent=1)


# ─────────────────────────── System Prompt（含技能索引） ───────────────────────────

def build_system_prompt() -> str:
    skills_lines = "\n".join(f"- {s['name']}: {s['description']}" for s in SKILLS_INDEX)
    return f"""你是 WeKnora 项目的深度调查 Agent。被分析项目（只读）：{SOURCE_ROOT}
Wiki：{WIKI_DIR}

## 可用技能（只列索引，正文按需加载）
{skills_lines}

开始一类新任务前，用 load_skill(name) 加载对应技能并按其步骤执行；
不要在没加载技能的情况下凭空发明方法论。

## 工作方式
- 多步任务先 todo_write 建计划；每段独立调查交给 spawn_subagent；
- 回答优先级：Wiki → 核实引用 → 源码搜索兜底；
- 简单问题直接查直接答。

## 回答结构（五节）
## 结论 / ## 调用链 / ## 相关页面 / ## 源文件引用 / ## 不确定事项

## 安全红线（权限层强制）
敏感文件连读都不允许；被分析项目严格只读。"""


# ─────────────────────────── LLM 客户端 ───────────────────────────

class OpenAILLM:
    def __init__(self, env: dict):
        from openai import OpenAI
        api_key = env.get("OPENAI_API_KEY")
        if not api_key or api_key.startswith("sk-your"):
            sys.exit("错误：未配置 OPENAI_API_KEY。复制 .env.example 为 .env 并填写，"
                     "或用 --mock 运行。")
        self.client = OpenAI(api_key=api_key,
                             base_url=env.get("OPENAI_BASE_URL") or None)
        self.model = env.get("MODEL_ID", "gpt-4o-mini")

    def chat(self, messages: list, tools: list):
        kwargs = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            kwargs["tools"] = tools
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message


def _mk_msg(round_no, text, calls):
    tool_calls = [SimpleNamespace(
        id=f"call_{round_no}_{i}", type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))
        for i, (name, args) in enumerate(calls)]
    return SimpleNamespace(content=text, tool_calls=tool_calls or None)


class MockParentLLM:
    """演示：技能加载 → 两次大块 read_file 撑大上下文 → （压缩触发）→
    压缩后凭备忘录继续（小 grep 核实）→ 收敛。压缩请求不推进脚本轮次。"""

    def __init__(self):
        self.round = 0

    def chat(self, messages: list, tools: list):
        last = messages[-1]["content"] if messages else ""
        if "【压缩任务】" in str(last):        # 压缩调用：mock 机械提取要点
            return SimpleNamespace(content=(
                "原始任务：梳理 Agent 引擎的迭代控制与收尾机制。\n"
                "已确认发现：\n"
                "- executeLoop 循环边界 state.CurrentRound < e.config.MaxIterations"
                "（internal/agent/engine.go:386）\n"
                "- iterOutcome 哨兵控制 continue/break（engine.go:435-448）\n"
                "- finalize.go 前 60 行为最终回答的流式输出与图片要求注入\n"
                "未完成：确认 handleMaxIterations 的定义位置与兜底策略。"),
                tool_calls=None)
        self.round += 1
        script = {
            1: ("加载调用链追踪技能。",
                [("load_skill", {"name": "trace-call-chain"})]),
            2: ("按技能步骤：先大块精读 engine.go 的循环区。",
                [("read_file", {"path": "internal/agent/engine.go",
                                "offset": 340, "limit": 300})]),
            3: ("再读 finalize.go 的收尾区。",
                [("read_file", {"path": "internal/agent/finalize.go",
                                "offset": 1, "limit": 200})]),
            4: ("（此轮开始前应已触发压缩）凭备忘录里的发现继续：只差 "
                "handleMaxIterations 的精确位置，小 grep 核实，不重读大文件。",
                [("grep", {"pattern": "func .*handleMaxIterations",
                           "path": "internal/agent", "include": "*.go"})]),
        }
        if self.round in script:
            text, calls = script[self.round]
            return _mk_msg(self.round, text, calls)
        return _mk_msg(self.round,
            "## 结论\n（mock）迭代控制：executeLoop 以 CurrentRound < MaxIterations 为界，"
            "iterOutcome 哨兵决定去留；收尾：正常路径 finalize 流式输出，耗尽路径 "
            "handleMaxIterations（finalize.go:160，压缩后用一次小 grep 核实，"
            "未重读任何大文件）。\n\n## 调用链\nexecuteLoop → runReActIteration → "
            "finalize / handleMaxIterations\n\n## 相关页面\ninternal-agent\n\n"
            "## 源文件引用\n- internal/agent/engine.go:386（来自压缩备忘录）\n"
            "- internal/agent/finalize.go:160（压缩后 grep 核实）\n\n"
            "## 不确定事项\n- 压缩备忘录声称 engine.go:435-448 是 iterOutcome——"
            "此条来自压缩前的阅读，未在压缩后二次核实。", [])


class MockChildLLM:
    def __init__(self, task: str):
        self.task = task
        self.round = 0

    def chat(self, messages: list, tools: list):
        self.round += 1
        if self.round == 1:
            return _mk_msg(1, "查看任务相关文件。",
                           [("grep", {"pattern": self.task[:20], "path": "internal"})])
        return _mk_msg(self.round, json.dumps(
            {"summary": "（mock 子 Agent）", "important_files": [],
             "call_chains": [], "uncertainties": []}, ensure_ascii=False), [])


# ─────────────────────────── Agent Loop（唯一改动：压缩钩子） ───────────────────────────

PARENT_TOOLS = set(TOOL_REGISTRY)


def agent_loop(question: str, llm, max_iterations: int, compress_threshold: int) -> str:
    tools = [entry["schema"] for entry in TOOL_REGISTRY.values()]
    messages = [{"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": question}]
    for round_no in range(1, max_iterations + 1):
        # 压缩钩子：调用前检查预算（对照 consolidator 在每轮之间触发）
        est = estimate_tokens(messages)
        if est > compress_threshold:
            messages = compress_history(messages, llm, question, compress_threshold)

        print(f"\n───── 第 {round_no}/{max_iterations} 轮（~{estimate_tokens(messages)} tok） ─────")
        msg = llm.chat(messages, tools)
        if msg.content:
            print(f"[模型] {msg.content}")
        messages.append({
            "role": "assistant", "content": msg.content or "",
            **({"tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls]} if msg.tool_calls else {}),
        })
        if not msg.tool_calls:
            return msg.content or "（模型没有给出内容）"
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            print(f"[工具] {tc.function.name}({json.dumps(args, ensure_ascii=False)[:110]})")
            result = execute_tool(tc.function.name, args, PARENT_TOOLS)
            preview = result if len(result) <= 260 else result[:260] + " ..."
            print(f"[输出] {preview}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return f"[护栏] 达到最大迭代数 {max_iterations}，强制停止。"


def main():
    global SUBAGENT_LLM_FACTORY
    parser = argparse.ArgumentParser(description="s11：上下文工程（Skill + 压缩）")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("query", help="深度调查（技能按需加载 + 历史压缩）")
    p.add_argument("question")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS_DEFAULT)
    p.add_argument("--compress-threshold", type=int, default=COMPRESS_THRESHOLD_DEFAULT,
                   help=f"估算 token 超过即压缩（默认 {COMPRESS_THRESHOLD_DEFAULT}；demo 用 3000）")
    args = parser.parse_args()

    print(f"已注册工具：{', '.join(TOOL_REGISTRY)}")
    print(f"技能索引（{len(SKILLS_INDEX)} 个，只载名称+一行说明）："
          f"{', '.join(s['name'] for s in SKILLS_INDEX)}")
    if args.mock:
        llm = MockParentLLM()
        SUBAGENT_LLM_FACTORY = lambda task: MockChildLLM(task)
    else:
        env = load_env()
        llm = OpenAILLM(env)
        SUBAGENT_LLM_FACTORY = lambda task: OpenAILLM(env)
    answer = agent_loop(args.question, llm, args.max_iterations, args.compress_threshold)
    print(f"\n═════ 最终回答 ═════\n{answer}")


if __name__ == "__main__":
    main()
