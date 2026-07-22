# -*- coding: utf-8 -*-
"""
s10：Todo 与子 Agent

跨模块调用链追踪（如「上传文档到向量入库的完整链路」）会遇到两个瓶颈：
  1. **忘**：十几轮探索后，模型忘了自己查到哪一步、还剩什么没查；
  2. **胀**：每一步的 grep/read 原始输出都堆在同一个上下文里，很快逼近上限。

s10 各给一个工具：

  Todo（对照 tools/todo_write.go）——对抗「忘」：
    todo_write(items)        制定计划（**计划内容由模型生成，代码不写死**）
    todo_read()              查看当前计划
    todo_update(id, status)  推进状态：pending / in_progress / completed / blocked
    列表持久化到 state/session.json（Agent 的工作状态也遵循「状态外置」）。

  子 Agent（对照 learn-claude-code v3）——对抗「胀」：
    spawn_subagent(task)     开一个**独立上下文**的子 Agent 去执行子任务：
      - 不继承父 Agent 的对话历史（只拿到任务描述）；
      - 工具集只读且**没有 spawn_subagent**（深度=1，防递归）；
      - 探索的原始输出留在子上下文里，返回给父 Agent 的只有结构化结果：
          {summary, important_files, call_chains, uncertainties}
    父 Agent 的上下文只涨「结论」，不涨「过程」——这就是上下文隔离。

两个工具合起来就是跨模块追踪的标准姿势：
  todo 拆解链路 → 每段 spawn 一个子 Agent → 父 Agent 只做调度与综合。

对照 WeKnora：
  - tools/todo_write.go：同样强调「一次只有一个 in_progress」「完成才标 completed」
    ——这些纪律写在工具描述里（喂给模型），不是写在代码里；
  - WeKnora 没有通用 spawn_subagent（它的 Agent 面向问答，深探索靠
    wiki_read_source_doc 等专用工具收敛上下文）；子 Agent 的参照物是
    learn-claude-code v3（v3_subagent.py）：同样的「独立上下文 + 结构化返回」。

用法：
    python code.py query "从上传一个文档到向量入库，WeKnora 的完整处理链路是什么？"
    python code.py query "..." --mock     # 脚本化演示（工具与子 Agent 真实执行）
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

# ─────────────────────────── 配置（同 s09） ───────────────────────────

SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", r"C:\Desktop\Project\WeKnora")).resolve()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = Path(os.environ.get("WIKI_WORKSPACE", PROJECT_ROOT / "workspace")).resolve()
WIKI_DIR = WORKSPACE / "wiki"
STATE_DIR = WORKSPACE / "state"

MAX_ITERATIONS_DEFAULT = 20
SUBAGENT_MAX_ITERATIONS = 8
MAX_TOOL_OUTPUT = 6000
BASH_TIMEOUT = 30
READ_DEFAULT_LIMIT = 200
READ_MAX_LIMIT = 500
GREP_MAX_RESULTS = 50
GLOB_MAX_RESULTS = 100
LIST_MAX_ENTRIES = 200
WIKI_SEARCH_MAX = 25

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


# ─────────────────────────── 权限层（同 s08/s09） ───────────────────────────

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


# ─────────────────────────── 工具注册表（同 s08/s09） ───────────────────────────

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
        return f"[拒绝] 工具 {name} 在当前上下文不可用（子 Agent 工具集受限）。"
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
    if len(result) > MAX_TOOL_OUTPUT:
        result = result[:MAX_TOOL_OUTPUT] + f"\n...[截断，原始输出 {len(result)} 字符]"
    return result


# ─────────────────────────── 源码 / Wiki 工具（同 s09，实现从略注释） ───────────────────────────

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


# ─────────────────────────── 新增：Todo 工具 ───────────────────────────
# 对照 tools/todo_write.go——注意纪律写在**工具描述**里（喂给模型），不写在代码里：
# 代码只管存取与校验，什么时候建计划/怎么拆任务由模型决定。

TODOS = []      # [{id, content, status, note}]


def _persist_todos():
    save_json(STATE_DIR / "session.json", {
        "todos": TODOS,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })


def _render_todos() -> str:
    if not TODOS:
        return "（计划为空）"
    lines = []
    for t in TODOS:
        note = f" —— {t['note']}" if t.get("note") else ""
        lines.append(f"{t['id']}. {TODO_ICONS[t['status']]} [{t['status']}] {t['content']}{note}")
    return "\n".join(lines)


@tool("todo_write",
      "制定/重建任务计划（覆盖整个列表，全部置为 pending）。适用于多步任务开工前。"
      "纪律：同一时刻只保持一个 in_progress；完成一项立刻标 completed；"
      "做不下去的标 blocked 并写明原因。",
      {"type": "object",
       "properties": {"items": {"type": "array", "items": {"type": "string"},
                                "description": "任务描述列表（按执行顺序）"}},
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


@tool("todo_update",
      "更新一个任务的状态（pending/in_progress/completed/blocked），可附备注。",
      {"type": "object",
       "properties": {"id": {"type": "integer"},
                      "status": {"type": "string",
                                 "enum": list(TODO_STATUSES)},
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


# ─────────────────────────── 新增：子 Agent ───────────────────────────

SUBAGENT_LLM_FACTORY = None          # main() 里注入：task -> LLM 实例

# 子 Agent 的工具集：只读探索，没有 todo（它只有一个任务）、没有 spawn（深度=1）
SUBAGENT_TOOLS = {"bash", "list_files", "read_file", "grep", "glob",
                  "list_wiki_pages", "read_wiki_page", "search_wiki"}

SUBAGENT_SYSTEM_PROMPT = f"""你是一个专注的代码调查子 Agent。被分析项目（只读）：{SOURCE_ROOT}
你会收到**一个**具体的调查任务。规则：
- 只做交给你的任务，不发散；用 grep/read_file/Wiki 工具快速定位证据；
- 每个结论都要有 文件路径:行号 支撑；
- 完成后，最终回答**只输出一个 JSON 对象**（无围栏无解释）：
  {{"summary": "2-4 句结论", "important_files": ["路径:行号", ...],
    "call_chains": ["A → B → C", ...], "uncertainties": ["...", ...]}}

安全红线：敏感文件连读都不允许；项目只读。"""


def _validate_subagent_result(data) -> bool:
    return (isinstance(data, dict)
            and isinstance(data.get("summary"), str) and data["summary"].strip()
            and all(isinstance(data.get(k), list)
                    for k in ("important_files", "call_chains", "uncertainties")))


def run_subagent(task: str, context: str) -> dict:
    """独立上下文跑一个子 Agent。返回结构化 dict——父 Agent 只看到这个。"""
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
    else:
        final = ""

    # 解析结构化返回；失败就把原文包进 summary（子 Agent 的劳动不白费）
    text = final.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rstrip("`").rstrip()
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
      "派生一个独立上下文的子 Agent 执行单个调查子任务（如追踪某段调用链）。"
      "子 Agent 看不到你的对话历史——任务描述必须自包含。"
      "返回 {summary, important_files, call_chains, uncertainties}。"
      "适合：把大任务的每一段分给一个子 Agent，你只做调度与综合。",
      {"type": "object",
       "properties": {"task": {"type": "string", "description": "自包含的调查任务"},
                      "context": {"type": "string", "description": "可选补充背景"}},
       "required": ["task"]})
def tool_spawn_subagent(task: str, context: str = "") -> str:
    print(f"    ┌─ 子 Agent 启动：{task[:80]}")
    result = run_subagent(task, context)
    print(f"    └─ 子 Agent 返回（summary {len(result['summary'])} 字符，"
          f"文件 {len(result['important_files'])}，链 {len(result['call_chains'])}）")
    return json.dumps(result, ensure_ascii=False, indent=1)


# ─────────────────────────── System Prompt（父 Agent） ───────────────────────────

SYSTEM_PROMPT = f"""你是 WeKnora 项目的深度调查 Agent。被分析项目（只读）：{SOURCE_ROOT}
Wiki：{WIKI_DIR}（可用 search_wiki / read_wiki_page 导航）

## 多步任务的工作方式
1. 先用 todo_write 把任务拆成有序步骤（计划由你定，随进展可重建）；
2. 逐项推进：开工标 in_progress（同时只一个），完成标 completed，
   做不下去标 blocked 并写原因；
3. 每一段独立的调查交给 spawn_subagent——子 Agent 有独立上下文，
   返回结构化结论；你负责调度与综合，不要自己陷进某一段的细节里；
4. 简单问题不需要 todo 也不需要子 Agent，直接查直接答。

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
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, temperature=0)
        return resp.choices[0].message


def _mk_msg(round_no, text, calls):
    tool_calls = [SimpleNamespace(
        id=f"call_{round_no}_{i}", type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))
        for i, (name, args) in enumerate(calls)]
    return SimpleNamespace(content=text, tool_calls=tool_calls or None)


class MockParentLLM:
    """父 Agent 脚本：建计划 → 逐项推进 + 两个子 Agent → blocked 演示 → 综合。"""

    def __init__(self):
        self.round = 0

    def chat(self, messages: list, tools: list):
        self.round += 1
        script = {
            1: ("多段链路，先建计划。",
                [("todo_write", {"items": [
                    "定位上传入口：HTTP handler 到 service 层",
                    "追踪 docreader gRPC 解析在哪里被调用",
                    "追踪切分 → Embedding → 向量入库",
                    "综合各段成完整链路"]})]),
            2: ("第 1 段交给子 Agent。",
                [("todo_update", {"id": 1, "status": "in_progress"}),
                 ("spawn_subagent", {"task":
                     "在 WeKnora 中定位「上传文档」的入口调用链：从 HTTP handler 的 "
                     "CreateKnowledgeFromFile 到 service 层的同名方法。"
                     "给出文件:行号。提示：internal/handler 与 "
                     "internal/application/service。"})]),
            3: ("第 1 段完成。第 2 段交给下一个子 Agent。",
                [("todo_update", {"id": 1, "status": "completed"}),
                 ("todo_update", {"id": 2, "status": "in_progress"}),
                 ("spawn_subagent", {"task":
                     "在 WeKnora 中定位 docreader（Python gRPC 文档解析服务）的 Go 侧"
                     "客户端在哪里创建、在哪个文件实现 gRPC 调用。给出文件:行号。"
                     "提示：搜 NewDocReaderClient。"})]),
            4: ("第 2 段完成。第 3 段涉及 chunk/embedding/向量库注册表，"
                "mock 轮次预算内做不完——标 blocked 留待真实运行。",
                [("todo_update", {"id": 2, "status": "completed"}),
                 ("todo_update", {"id": 3, "status": "blocked",
                                  "note": "mock 演示预算内不展开；真实 LLM 会再派子 Agent"}),
                 ("todo_update", {"id": 4, "status": "in_progress"})]),
        }
        if self.round in script:
            text, calls = script[self.round]
            return _mk_msg(self.round, text, calls)
        return _mk_msg(self.round,
            "## 结论\n（mock 综合两个子 Agent 的结构化返回）上传链路前两段：HTTP 入口 "
            "KnowledgeHandler.CreateKnowledgeFromFile → knowledgeService.CreateKnowledgeFromFile"
            "；文档解析走 docreader 的 gRPC 客户端（grpc_parser.go）。\n\n"
            "## 调用链\nPOST /knowledge/file → handler/knowledge.go:310 → "
            "service/knowledge_create.go:27 → …… → docparser/grpc_parser.go:75"
            "（NewDocReaderClient）→ Python docreader\n\n"
            "## 相关页面\n（Wiki 尚未覆盖上传链路——真实运行时子 Agent 会先查 Wiki）\n\n"
            "## 源文件引用\n见两个子 Agent 返回的 important_files（已在对话历史中）。\n\n"
            "## 不确定事项\n- 任务 3（切分→Embedding→向量入库）标记 blocked，未追踪；\n"
            "- 父 Agent 上下文只包含两个子 Agent 的结构化摘要，原始探索输出隔离在子上下文。", [])


class MockChildLLM:
    """子 Agent 脚本（按任务关键词分流），真实执行 grep/read_file 后返回结构化 JSON。"""

    def __init__(self, task: str):
        self.task = task
        self.round = 0

    def chat(self, messages: list, tools: list):
        self.round += 1
        if "CreateKnowledgeFromFile" in self.task or "上传" in self.task:
            script = {
                1: ("定位 handler。",
                    [("grep", {"pattern": r"func .*CreateKnowledgeFromFile",
                               "path": "internal", "include": "*.go"})]),
                2: ("确认 handler → service 的调用。",
                    [("read_file", {"path": "internal/handler/knowledge.go",
                                    "offset": 310, "limit": 12})]),
            }
            final = {"summary": "上传入口是 KnowledgeHandler.CreateKnowledgeFromFile"
                                "（gin 路由处理器），它调用 service 层 "
                                "knowledgeService.CreateKnowledgeFromFile 完成创建。",
                     "important_files": ["internal/handler/knowledge.go:310",
                                         "internal/application/service/knowledge_create.go:27"],
                     "call_chains": ["POST /knowledge/file → KnowledgeHandler."
                                     "CreateKnowledgeFromFile → knowledgeService."
                                     "CreateKnowledgeFromFile"],
                     "uncertainties": ["路由注册的确切路径未核实（在 router 层）"]}
        else:
            script = {
                1: ("搜 gRPC 客户端创建点。",
                    [("grep", {"pattern": "NewDocReaderClient",
                               "path": "internal", "include": "*.go"})]),
                2: ("看 grpc_parser 的客户端字段与创建。",
                    [("read_file", {"path": "internal/infrastructure/docparser/grpc_parser.go",
                                    "offset": 70, "limit": 10})]),
            }
            final = {"summary": "docreader 的 Go 侧客户端在 container 注册"
                                "（initDocReaderClient），gRPC 实现在 docparser/"
                                "grpc_parser.go：proto.NewDocReaderClient 建立连接。",
                     "important_files": ["internal/container/container.go:1422",
                                         "internal/infrastructure/docparser/grpc_parser.go:75"],
                     "call_chains": ["service → interfaces.DocumentReader → "
                                     "docparser.grpc_parser → Python docreader (gRPC)"],
                     "uncertainties": ["多种 DOCREADER_TRANSPORT 分支未逐一核实"]}
        if self.round in script:
            text, calls = script[self.round]
            return _mk_msg(self.round, text, calls)
        return _mk_msg(self.round, json.dumps(final, ensure_ascii=False), [])


# ─────────────────────────── Agent Loop（与 s07-s09 相同，多了 allowed 集合） ───────────────────────────

PARENT_TOOLS = set(TOOL_REGISTRY)      # 父 Agent 全量工具


def agent_loop(question: str, llm, max_iterations: int) -> str:
    tools = [entry["schema"] for entry in TOOL_REGISTRY.values()]
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question}]
    for round_no in range(1, max_iterations + 1):
        print(f"\n───── 第 {round_no}/{max_iterations} 轮 ─────")
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
            preview = result if len(result) <= 300 else result[:300] + " ..."
            print(f"[输出] {preview}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return f"[护栏] 达到最大迭代数 {max_iterations}，强制停止。"


def main():
    global SUBAGENT_LLM_FACTORY
    parser = argparse.ArgumentParser(description="s10：Todo 与子 Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("query", help="深度调查（todo 规划 + 子 Agent 分段执行）")
    p.add_argument("question")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS_DEFAULT)
    args = parser.parse_args()

    print(f"已注册工具：{', '.join(TOOL_REGISTRY)}")
    if args.mock:
        llm = MockParentLLM()
        SUBAGENT_LLM_FACTORY = lambda task: MockChildLLM(task)
    else:
        env = load_env()
        llm = OpenAILLM(env)
        SUBAGENT_LLM_FACTORY = lambda task: OpenAILLM(env)
    answer = agent_loop(args.question, llm, args.max_iterations)
    print(f"\n═════ 最终回答 ═════\n{answer}")
    if TODOS:
        print(f"\n═════ 最终计划状态（state/session.json） ═════\n{_render_todos()}")


if __name__ == "__main__":
    main()
