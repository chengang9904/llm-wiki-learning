# -*- coding: utf-8 -*-
"""
s12：Fixer Agent 与完整整合 —— 课程收官

两件事：

1. **Fixer Agent**：`python code.py fix` 消费 s06 lint 产出的 state/issues.json，
   逐条定夺：重读来源 → 修复（replace_in_page 最小修改）/ 页面标 deprecated /
   报告无法修复（update_issue ignored + 原因）。

   注意这里的**双范式协作结构**——外层循环是代码（逐条取 issue、失败隔离、
   限额），内层判断是 Agent（这条 issue 到底该怎么办）。这正是全课程的终点站：
   不是「用哪个范式」，而是「在哪个边界上交接」。

2. **六命令整合**（+ status 共七个）：
     init    初始化 workspace 骨架            （代码）
     ingest  全量生成：s03 map-reduce → s04 后处理     （流水线）
     update  增量更新：s05 git diff → s04 后处理       （流水线）
     lint    质量检查 → issues.json                    （流水线）
     query   基于 Wiki + 源码回答（s11 全能力）        （Agent）
     fix     消费 issues.json 逐条修复                 （Agent）
     status  汇总所有状态                              （代码）
   流水线命令通过子进程调用兄弟阶段的 code.py（s03/s04/s05/s06 已各自可运行、
   幂等、可续跑——整合层只做调度，不重写实现）；Agent 命令在本文件内实现。

对照 WeKnora：
  - builtin-wiki-fixer（types/custom_agent.go:30）：内置修复 Agent；:554 的注释
    特意说明它被排除出普通 agent 列表——修复是运维动作，不是聊天选项；
  - wiki_flag_issue / wiki_read_issue / wiki_update_issue：issue 的标记/读取/定夺
    三件套；update 的状态枚举 resolved/ignored/pending（教学版完全对齐）；
  - wiki_replace_text.go：slug + old_text + new_text 的精确替换，找不到原文时
    报错「Ensure you copy it exactly」——教学版连报错文案的设计都保留了：
    这个报错是喂给模型的操作指导。

用法：
    python code.py init | ingest [--mock ...] | update [--mock ...] | lint
                 | query "问题" [--mock] | fix [--mock] [--limit N] | status
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

STAGES = {                       # 流水线命令 → 兄弟阶段
    "ingest": PROJECT_ROOT / "s03_map_reduce" / "code.py",
    "postprocess": PROJECT_ROOT / "s04_postprocess" / "code.py",
    "update": PROJECT_ROOT / "s05_incremental_update" / "code.py",
    "lint": PROJECT_ROOT / "s06_wiki_lint" / "code.py",
}

MAX_ITERATIONS_DEFAULT = 20
FIXER_MAX_ITERATIONS = 10
COMPRESS_THRESHOLD_DEFAULT = 24000
COMPRESS_KEEP_TAIL = 3
SUBAGENT_MAX_ITERATIONS = 8
BASH_TIMEOUT = 30
READ_DEFAULT_LIMIT = 200
READ_MAX_LIMIT = 500
GREP_MAX_RESULTS = 50
GLOB_MAX_RESULTS = 100
LIST_MAX_ENTRIES = 200
WIKI_SEARCH_MAX = 25
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
ISSUE_RESOLUTIONS = ("resolved", "ignored", "pending")   # 对照 wiki_update_issue.go:32


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


def append_log(page_rel: str, reason: str, uncertainties: int = 0):
    log_path = WORKSPACE / "logs" / "wiki-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        if f.tell() == 0:
            f.write("# Wiki 修改日志\n\n")
        f.write(f"- {now} | {page_rel} | 原因: {reason} | 未确认事项: {uncertainties}\n")


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
        result = result[:budget] + f"\n...[截断至 {budget} 字符，原始 {len(result)}]"
    return result


# ─────────────────────────── 源码 / Wiki / Todo / Skill 工具（同 s11） ───────────────────────────

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


@tool("todo_write", "制定/重建任务计划（覆盖整个列表）。同时只一个 in_progress。",
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


def load_skills_index() -> list:
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


@tool("load_skill", "加载技能全文（system prompt 里只有索引）。",
      {"type": "object", "properties": {"name": {"type": "string"}},
       "required": ["name"]})
def tool_load_skill(name: str) -> str:
    name = name.strip().lower()
    for s in SKILLS_INDEX:
        if s["name"].lower() == name or s["file"].stem.lower() == name:
            text = s["file"].read_text(encoding="utf-8")
            body = text.split("---", 2)[2].strip() if text.startswith("---") else text
            return f"[技能 {s['name']} 已加载]\n\n{body}"
    return f"[错误] 技能不存在：{name}。可用：" + ", ".join(s["name"] for s in SKILLS_INDEX)


# ─────────────────────────── 新增：issue 三件套 + 页面精确替换 ───────────────────────────

def _load_issues():
    return load_json(STATE_DIR / "issues.json", {"next_id": 1, "issues": []})


@tool("read_issue", "读取一条 issue 的完整信息。",
      {"type": "object", "properties": {"issue_id": {"type": "integer"}},
       "required": ["issue_id"]})
def tool_read_issue(issue_id: int) -> str:
    for issue in _load_issues()["issues"]:
        if issue["id"] == issue_id:
            return json.dumps(issue, ensure_ascii=False, indent=1)
    return f"[错误] 没有 id={issue_id} 的 issue"


@tool("update_issue",
      "定夺一条 issue：resolved（已修复）/ ignored（无法修复或不应修复，note 必须写原因）"
      "/ pending（留待人工）。这是每条 issue 处理的收尾动作。",
      {"type": "object",
       "properties": {"issue_id": {"type": "integer"},
                      "status": {"type": "string", "enum": list(ISSUE_RESOLUTIONS)},
                      "note": {"type": "string", "description": "定夺理由（ignored 时必填）"}},
       "required": ["issue_id", "status"]})
def tool_update_issue(issue_id: int, status: str, note: str = "") -> str:
    if status not in ISSUE_RESOLUTIONS:
        return f"[错误] status 必须是 {'/'.join(ISSUE_RESOLUTIONS)}"
    if status == "ignored" and not note.strip():
        return "[错误] ignored 必须在 note 里写明原因"
    store = _load_issues()
    for issue in store["issues"]:
        if issue["id"] == issue_id:
            issue["status"] = status
            issue["resolution_note"] = note
            issue["resolved_by"] = "fixer-agent"
            issue["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            save_json(STATE_DIR / "issues.json", store)
            return f"[已定夺] issue #{issue_id} → {status}" + (f"（{note}）" if note else "")
    return f"[错误] 没有 id={issue_id} 的 issue"


@tool("flag_issue",
      "标记一个新发现的 Wiki 问题（如问答/核实过程中发现页面与源码不一致）。",
      {"type": "object",
       "properties": {"slug": {"type": "string"},
                      "issue_type": {"type": "string"},
                      "description": {"type": "string"}},
       "required": ["slug", "issue_type", "description"]})
def tool_flag_issue(slug: str, issue_type: str, description: str) -> str:
    store = _load_issues()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    issue = {"id": store["next_id"], "type": issue_type, "severity": "warning",
             "slug": slug, "page": "", "description": description,
             "evidence": "", "status": "open", "detected_by": "agent",
             "created_at": now, "updated_at": now}
    store["issues"].append(issue)
    store["next_id"] += 1
    save_json(STATE_DIR / "issues.json", store)
    return f"[已标记] issue #{issue['id']}（{issue_type} @ {slug}）"


@tool("replace_in_page",
      "在 Wiki 页面里做精确文本替换（old_text 必须与页面内容逐字一致，含空白）。"
      "这是修复页面的唯一写入手段——最小修改，不要整页重写。"
      "替换成功后自动更新 updated_at 并写修改日志。",
      {"type": "object",
       "properties": {"slug": {"type": "string"},
                      "old_text": {"type": "string"},
                      "new_text": {"type": "string"},
                      "reason": {"type": "string", "description": "修改原因（进日志）"}},
       "required": ["slug", "old_text", "new_text"]})
def tool_replace_in_page(slug: str, old_text: str, new_text: str, reason: str = "") -> str:
    p = _find_page_file(slug.strip().lower())
    if p is None:
        return f"[错误] 页面不存在：{slug}"
    text = p.read_text(encoding="utf-8")
    if old_text not in text:
        # 对照 wiki_replace_text.go:84——这个报错是喂给模型的操作指导
        return ("[错误] old_text 在页面中找不到。必须逐字复制页面里的原文"
                "（含空格与标点）。先 read_wiki_page 确认。")
    if text.count(old_text) > 1:
        return f"[错误] old_text 在页面中出现 {text.count(old_text)} 次，无法唯一定位。请扩大选段。"
    text = text.replace(old_text, new_text, 1)
    today = datetime.date.today().isoformat()
    text = re.sub(r"^updated_at: .*$", f"updated_at: {today}", text, flags=re.MULTILINE)
    p.write_text(text, encoding="utf-8", newline="\n")
    append_log(p.relative_to(WORKSPACE).as_posix(),
               f"fixer 修改（{reason or '未注明'}）")
    # 状态同步：frontmatter 的来源可能被修改，pages.json 跟着刷新
    meta = _pages_meta()
    entry = meta["pages"].get(p.stem)
    if entry is not None:
        front = text.split("---", 2)[1] if text.startswith("---") else ""
        entry["sources"] = sorted({m.group(1).strip() for m in re.finditer(
            r"-\s*\{path:\s*([^,}]+),", front)})
        entry["updated_at"] = today
        save_json(STATE_DIR / "pages.json", meta)
    return f"[已替换] {p.name}：{len(old_text)} → {len(new_text)} 字符，updated_at → {today}"


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


# ─────────────────────────── 通用 Agent Loop（同 s11，含压缩钩子） ───────────────────────────

def estimate_tokens(messages: list) -> int:
    total = 0
    for m in messages:
        total += len(str(m.get("content", "")))
        for tc in m.get("tool_calls", []) or []:
            total += len(str(tc))
    return total // 3


def _safe_tail_start(messages: list, keep: int) -> int:
    start = max(2, len(messages) - keep)
    while start < len(messages) and messages[start]["role"] == "tool":
        start += 1
    return start


COMPRESS_PROMPT = """【压缩任务】把下面的 Agent 对话历史压缩成工作备忘录：
1 原始任务；2 已确认关键发现（带文件:行号）；3 未完成事项。
不要保留工具原始输出。直接输出备忘录正文。

<history>
{history}
</history>"""


def compress_history(messages: list, llm, question: str, threshold: int) -> list:
    tail_start = _safe_tail_start(messages, COMPRESS_KEEP_TAIL)
    middle = messages[1:tail_start]
    if len(middle) < 4:
        return messages
    serialized = []
    for m in middle:
        content = str(m.get("content", ""))[:1200]
        calls = "".join(f" [调用 {tc['function']['name']}]"
                        for tc in m.get("tool_calls", []) or [])
        serialized.append(f"({m['role']}){calls} {content}")
    req = [{"role": "system", "content": "你是对话压缩器，只输出备忘录正文。"},
           {"role": "user", "content": COMPRESS_PROMPT.format(history="\n".join(serialized))}]
    msg = llm.chat(req, [])
    summary = (msg.content or "").strip() or "（压缩失败）"
    before = estimate_tokens(messages)
    new_messages = [messages[0],
                    {"role": "user", "content":
                        f"原始任务：{question}\n\n【压缩备忘录，关键发现直接使用，"
                        f"不要重扫】\n\n{summary}"},
                    *messages[tail_start:]]
    print(f"    [压缩] ~{before} tok > {threshold} → ~{estimate_tokens(new_messages)} tok")
    return new_messages


def agent_loop(question: str, llm, system_prompt: str, allowed_tools: set,
               max_iterations: int, compress_threshold: int = COMPRESS_THRESHOLD_DEFAULT,
               indent: str = "") -> str:
    tools = [TOOL_REGISTRY[n]["schema"] for n in TOOL_REGISTRY if n in allowed_tools]
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": question}]
    for round_no in range(1, max_iterations + 1):
        if estimate_tokens(messages) > compress_threshold:
            messages = compress_history(messages, llm, question, compress_threshold)
        print(f"{indent}───── 第 {round_no}/{max_iterations} 轮 ─────")
        msg = llm.chat(messages, tools)
        if msg.content:
            print(f"{indent}[模型] {msg.content[:250]}")
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
            print(f"{indent}[工具] {tc.function.name}"
                  f"({json.dumps(args, ensure_ascii=False)[:110]})")
            result = execute_tool(tc.function.name, args, allowed_tools)
            preview = result if len(result) <= 220 else result[:220] + " ..."
            print(f"{indent}[输出] {preview}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return f"[护栏] 达到最大迭代数 {max_iterations}，强制停止。"


# ─────────────────────────── 子 Agent（query 用，同 s11） ───────────────────────────

SUBAGENT_LLM_FACTORY = None
SUBAGENT_TOOLS = {"bash", "list_files", "read_file", "grep", "glob",
                  "list_wiki_pages", "read_wiki_page", "search_wiki", "load_skill"}


@tool("spawn_subagent", "派生独立上下文的子 Agent 执行单个调查子任务（描述须自包含）。",
      {"type": "object",
       "properties": {"task": {"type": "string"}, "context": {"type": "string"}},
       "required": ["task"]})
def tool_spawn_subagent(task: str, context: str = "") -> str:
    llm = SUBAGENT_LLM_FACTORY(task)
    user = task if not context else f"{task}\n\n补充背景：{context}"
    sub_prompt = (f"你是专注的代码调查子 Agent。项目（只读）：{SOURCE_ROOT}\n"
                  "只做交给你的任务；结论带 文件:行号；最终只输出 JSON："
                  '{"summary": "...", "important_files": [...], '
                  '"call_chains": [...], "uncertainties": [...]}')
    print(f"    ┌─ 子 Agent：{task[:70]}")
    final = agent_loop(user, llm, sub_prompt, SUBAGENT_TOOLS,
                       SUBAGENT_MAX_ITERATIONS, indent="    │ ")
    print("    └─ 子 Agent 返回")
    start, end = final.find("{"), final.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(final[start:end + 1])
            if isinstance(data, dict) and data.get("summary"):
                return json.dumps(data, ensure_ascii=False, indent=1)
        except json.JSONDecodeError:
            pass
    return json.dumps({"summary": final, "important_files": [], "call_chains": [],
                       "uncertainties": ["未按 JSON 结构返回"]}, ensure_ascii=False)


# ─────────────────────────── query（s11 全能力 + flag_issue） ───────────────────────────

QUERY_TOOLS = {"bash", "list_files", "read_file", "grep", "glob",
               "list_wiki_pages", "read_wiki_page", "search_wiki", "load_skill",
               "todo_write", "todo_read", "todo_update", "spawn_subagent",
               "flag_issue"}


def build_query_prompt() -> str:
    skills_lines = "\n".join(f"- {s['name']}: {s['description']}" for s in SKILLS_INDEX)
    return f"""你是 WeKnora 项目的 Wiki 问答 Agent。项目（只读）：{SOURCE_ROOT}
Wiki：{WIKI_DIR}

## 可用技能（按需 load_skill）
{skills_lines}

## 工作方式
- 回答优先级：Wiki（search_wiki→read_wiki_page）→ read_file 核实引用 → 源码搜索兜底；
- 多步任务先 todo_write；独立段交给 spawn_subagent；
- **发现 Wiki 与源码不一致：用 flag_issue 标记**（Fixer 会消费），并在回答中说明。

## 回答结构（五节）
## 结论 / ## 调用链 / ## 相关页面 / ## 源文件引用 / ## 不确定事项

## 安全红线（权限层强制）
敏感文件连读都不允许；被分析项目严格只读。"""


# ─────────────────────────── fix：Fixer Agent ───────────────────────────
# 外层循环是代码（逐条取 issue、失败隔离、限额）——流水线原则；
# 内层判断是 Agent（这条 issue 该修、该弃、还是该报告）——Agent 原则。
# 这是双范式的交接点在代码里的样子。

FIXER_TOOLS = {"read_wiki_page", "list_wiki_pages", "search_wiki",
               "read_file", "grep", "glob", "load_skill",
               "read_issue", "update_issue", "flag_issue", "replace_in_page"}

FIXER_LLM_FACTORY = None

FIXER_SYSTEM_PROMPT = f"""你是 Wiki 修复 Agent（对照 WeKnora 的 builtin-wiki-fixer）。
项目（只读）：{SOURCE_ROOT}；Wiki：{WIKI_DIR}

你一次处理**一条** issue。流程纪律：
1. 先 load_skill("fix-wiki-page") 拿决策树（不要凭空发明修法）；
2. read_wiki_page 读页面现状；**必须重读相关来源**（read_file/grep）再下结论——
   绝不允许只看 issue 描述就改页面；
3. 三种出路（必须以 update_issue 收尾）：
   - 能修：replace_in_page 最小修改（只动相关行，reason 带 issue id）→
     update_issue(resolved, 说明改了什么)；
   - 页面整体失效：replace_in_page 把 status 改为 deprecated（保留文件）→
     update_issue(resolved, 注明已废弃)；
   - 不能修/不该修（如结构性问题应重跑后处理）：update_issue(ignored, 原因必填)。

## 安全红线（权限层强制）
敏感文件连读都不允许；被分析项目严格只读；页面修改只用 replace_in_page。"""


def cmd_fix(args):
    global FIXER_LLM_FACTORY
    store = _load_issues()
    open_issues = [i for i in store["issues"] if i["status"] == "open"]
    open_issues.sort(key=lambda i: ({"error": 0, "warning": 1, "info": 2}
                                    .get(i["severity"], 3), i["id"]))
    if args.limit:
        open_issues = open_issues[:args.limit]
    if not open_issues:
        print("没有 open 状态的 issue。先运行 lint。")
        return
    print(f"[fix] 待处理 {len(open_issues)} 条（按严重度排序）\n")

    if args.mock:
        FIXER_LLM_FACTORY = lambda issue: MockFixerLLM(issue)
    else:
        env = load_env()
        FIXER_LLM_FACTORY = lambda issue: OpenAILLM(env)

    results = {"resolved": 0, "ignored": 0, "unresolved": 0}
    for issue in open_issues:
        print(f"┏━━ issue #{issue['id']} [{issue['severity']}] {issue['type']} "
              f"@ {issue['slug']} ━━")
        task = ("处理这条 Wiki issue（处理完必须调用 update_issue 定夺）：\n"
                + json.dumps(issue, ensure_ascii=False, indent=1))
        llm = FIXER_LLM_FACTORY(issue)
        try:
            agent_loop(task, llm, FIXER_SYSTEM_PROMPT, FIXER_TOOLS,
                       FIXER_MAX_ITERATIONS, indent="┃ ")
        except Exception as e:                       # 失败隔离：一条崩了不拖累整批
            print(f"┃ [异常] {type(e).__name__}: {e}")
        # 事后核对：Agent 是否真的定夺了（不信任自觉，验证状态）
        after = next(i for i in _load_issues()["issues"] if i["id"] == issue["id"])
        if after["status"] == "open":
            print(f"┗━━ issue #{issue['id']} 未定夺（仍为 open），留待下次/人工\n")
            results["unresolved"] += 1
        else:
            print(f"┗━━ issue #{issue['id']} → {after['status']}"
                  f"（{after.get('resolution_note', '')}）\n")
            results[after["status"] if after["status"] in results else "resolved"] += 1

    print(f"[fix 完成] resolved={results['resolved']} ignored={results['ignored']} "
          f"未定夺={results['unresolved']}")
    print("[提示] 结构性问题（orphan/backlink/index）建议重跑：python code.py ingest "
          "会带动 s04 后处理更新链接与索引")


class MockFixerLLM:
    """按 issue 类型分流的修复脚本（工具真实执行、页面真实修改）。"""

    def __init__(self, issue: dict):
        self.issue = issue
        self.round = 0

    def chat(self, messages: list, tools: list):
        self.round += 1
        t, iid, slug = self.issue["type"], self.issue["id"], self.issue["slug"]
        if t == "dead_source_ref":
            evidence = self.issue["evidence"]           # 如 docs/agent-design.md
            script = {
                1: ("先拿决策树。", [("load_skill", {"name": "fix-wiki-page"})]),
                2: ("读页面现状。", [("read_wiki_page", {"slug": slug})]),
                3: (f"重读来源：确认 {evidence} 在项目中确实不存在。",
                    [("glob", {"pattern": evidence})]),
                4: ("确认不存在且无同名文件可改指。按决策树：删除该来源引用行，"
                    "并定夺 resolved。",
                    [("replace_in_page", {
                        "slug": slug,
                        "old_text": f"  - {{path: {evidence}, lines: 1-10}}\n",
                        "new_text": "",
                        "reason": f"issue #{iid} dead_source_ref：来源已不存在"}),
                     ("update_issue", {
                         "issue_id": iid, "status": "resolved",
                         "note": f"已删除失效来源 {evidence} 的引用行。"
                                 "页面已无来源，后续 lint 会以 missing_sources 跟进"})]),
            }
            if self.round in script:
                text, calls = script[self.round]
                return _mk_msg(self.round, text, calls)
            return _mk_msg(self.round, f"issue #{iid} 已处理：删除失效引用并定夺 resolved。", [])
        if t in ("orphan_page", "missing_backlink", "index_missing"):
            script = {
                1: ("先拿决策树。", [("load_skill", {"name": "fix-wiki-page"})]),
                2: ("决策树明确：结构性问题不手工加链接，应重跑 s04 后处理。定夺 ignored。",
                    [("update_issue", {
                        "issue_id": iid, "status": "ignored",
                        "note": "结构性问题，按 fix-wiki-page 决策树应重跑 postprocess "
                                "而非手工修补"})]),
            }
            if self.round in script:
                text, calls = script[self.round]
                return _mk_msg(self.round, text, calls)
            return _mk_msg(self.round, f"issue #{iid} 定夺 ignored（建议重跑后处理）。", [])
        # 其余类型（stale_draft 等）：mock 无法做语义核实，如实定夺
        script = {
            1: ("读页面。", [("read_wiki_page", {"slug": slug})]),
            2: ("mock 无法做语义级核实（需要真实 LLM 按 verify-sources 比对）。"
                "如实定夺 ignored。",
                [("update_issue", {
                    "issue_id": iid, "status": "ignored",
                    "note": "mock 无法做语义核实；真实 LLM 会核实后改 verified"})]),
        }
        if self.round in script:
            text, calls = script[self.round]
            return _mk_msg(self.round, text, calls)
        return _mk_msg(self.round, f"issue #{iid} 已定夺。", [])


class MockQueryLLM:
    """query --mock：单轮演示（问答全流程演示见 s09/s10/s11）。"""

    def __init__(self):
        self.round = 0

    def chat(self, messages: list, tools: list):
        self.round += 1
        if self.round == 1:
            return _mk_msg(1, "查 Wiki。", [("search_wiki", {"query": "agent"})])
        return _mk_msg(self.round,
                       "## 结论\n（mock）s12 的 query 与 s11 同源；完整问答演示见 "
                       "s09/s10/s11 各自的 --mock。\n## 调用链\n—\n## 相关页面\n见上一轮"
                       "搜索结果\n## 源文件引用\n—\n## 不确定事项\n—", [])


# ─────────────────────────── 流水线命令：调度兄弟阶段 ───────────────────────────

def run_stage(stage: str, extra_args: list) -> int:
    """子进程调用兄弟阶段的 code.py。整合层只做调度——s03/s05/s06 的幂等、
    失败隔离、断点续跑原样生效，不在这里重写。"""
    script = STAGES[stage]
    cmd = [sys.executable, str(script)] + extra_args
    print(f"[dispatch] {script.parent.name} :: {' '.join(extra_args)}", flush=True)
    result = subprocess.run(cmd, cwd=script.parent)
    return result.returncode


def cmd_init():
    for sub in PAGE_TYPE_DIRS:
        (WIKI_DIR / sub).mkdir(parents=True, exist_ok=True)
    for d in (WORKSPACE / "raw", STATE_DIR, WORKSPACE / "logs"):
        d.mkdir(parents=True, exist_ok=True)
    for name, default in (("sources.json", {"files": {}}),
                          ("pages.json", {"pages": {}, "redirects": {}}),
                          ("pending.json", {"ops": [], "next_id": 1}),
                          ("issues.json", {"next_id": 1, "issues": []}),
                          ("session.json", {"todos": []})):
        path = STATE_DIR / name
        if not path.is_file():
            save_json(path, default)
    print(f"[init] workspace 就绪：{WORKSPACE}")


def cmd_status():
    sources = load_json(STATE_DIR / "sources.json", {"files": {}})
    pending = load_json(STATE_DIR / "pending.json", {"ops": []})
    pages = load_json(STATE_DIR / "pages.json", {"pages": {}})
    issues = load_json(STATE_DIR / "issues.json", {"issues": []})
    session = load_json(STATE_DIR / "session.json", {"todos": []})
    files = sources["files"]
    processed = sum(1 for f in files.values() if f.get("processed_hash") == f.get("hash"))
    commit = sources.get("last_ingest_commit", "（未记录）")
    ops = {}
    for op in pending["ops"]:
        ops[op["status"]] = ops.get(op["status"], 0) + 1
    iss = {}
    for i in issues["issues"]:
        iss[i["status"]] = iss.get(i["status"], 0) + 1
    print(f"workspace : {WORKSPACE}")
    print(f"manifest  : {len(files)} 个文件，已处理 {processed}")
    print(f"commit    : {commit[:12] if len(str(commit)) > 12 else commit}")
    print(f"op 队列   : {ops or '（空）'}")
    print(f"页面      : {len(pages['pages'])} 个"
          + ("".join(f"\n  {s} ← {len(m.get('sources', []))} 来源"
                     + ("（deprecated）" if m.get("status") == "deprecated" else "")
                     for s, m in sorted(pages["pages"].items()))))
    print(f"issues    : {iss or '（空）'}")
    print(f"todo      : {len(session.get('todos', []))} 项")


# ─────────────────────────── 入口 ───────────────────────────

def main():
    global SUBAGENT_LLM_FACTORY
    parser = argparse.ArgumentParser(
        description="s12：完整整合——init/ingest/update/lint 走流水线，query/fix 走 Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化 workspace（代码）")
    p_ing = sub.add_parser("ingest", help="全量生成：s03 map-reduce → s04 后处理（流水线）")
    p_ing.add_argument("--mock", action="store_true")
    p_ing.add_argument("--path")
    p_ing.add_argument("--limit", type=int, default=10)
    p_upd = sub.add_parser("update", help="增量更新：s05 git diff → s04 后处理（流水线）")
    p_upd.add_argument("--mock", action="store_true")
    p_upd.add_argument("--path")
    p_upd.add_argument("--limit", type=int, default=0)
    sub.add_parser("lint", help="质量检查 → issues.json（流水线，s06）")
    p_q = sub.add_parser("query", help="基于 Wiki + 源码回答（Agent，s11 全能力）")
    p_q.add_argument("question")
    p_q.add_argument("--mock", action="store_true")
    p_q.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS_DEFAULT)
    p_f = sub.add_parser("fix", help="消费 issues.json 逐条修复（Agent）")
    p_f.add_argument("--mock", action="store_true")
    p_f.add_argument("--limit", type=int, default=0, help="本次最多处理几条 issue")
    sub.add_parser("status", help="汇总全部状态（代码）")
    args = parser.parse_args()

    if args.command == "init":
        cmd_init()
    elif args.command == "ingest":
        extra = ["ingest"] + (["--mock"] if args.mock else []) \
            + (["--path", args.path] if args.path else []) + ["--limit", str(args.limit)]
        code = run_stage("ingest", extra)
        if code == 0:
            code = run_stage("postprocess",
                             ["postprocess"] + (["--mock"] if args.mock else []))
        sys.exit(code)
    elif args.command == "update":
        extra = ["update"] + (["--mock"] if args.mock else []) \
            + (["--path", args.path] if args.path else []) \
            + (["--limit", str(args.limit)] if args.limit else [])
        code = run_stage("update", extra)
        if code == 0:
            code = run_stage("postprocess",
                             ["postprocess"] + (["--mock"] if args.mock else []))
        sys.exit(code)
    elif args.command == "lint":
        sys.exit(run_stage("lint", ["lint"]))
    elif args.command == "query":
        if args.mock:
            llm = MockQueryLLM()
            SUBAGENT_LLM_FACTORY = lambda task: MockQueryLLM()
        else:
            env = load_env()
            llm = OpenAILLM(env)
            SUBAGENT_LLM_FACTORY = lambda task: OpenAILLM(env)
        answer = agent_loop(args.question, llm, build_query_prompt(),
                            QUERY_TOOLS, args.max_iterations)
        print(f"\n═════ 最终回答 ═════\n{answer}")
    elif args.command == "fix":
        cmd_fix(args)
    else:
        cmd_status()


if __name__ == "__main__":
    main()
