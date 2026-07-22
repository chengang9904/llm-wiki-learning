# -*- coding: utf-8 -*-
"""
s09：Wiki 问答 Agent

s08 的 Agent 只会从零 grep 源码——s01–s06 辛苦建好的 Wiki 完全没被用上。
s09 给 Agent 装上 Wiki 导航工具，并用 system prompt 规定**回答优先级**：

    1. 先查 Wiki（search_wiki / list_wiki_pages → read_wiki_page）
    2. 关键事实用 read_file 打开页面引用的 `路径:行号` 核实（引用可能过时）
    3. Wiki 覆盖不到的，再用 grep/glob 搜源码补充

为什么这个顺序？Wiki 是**预蒸馏的知识**：一次 read_wiki_page 顶几十次 grep，
且自带来源引用；但它可能过时（s05 之后没 update 过），所以关键事实要回源码核实；
源码永远是最终事实。这正是「Wiki 优先、引用下钻、搜索兜底」的三级瀑布。

回答结构固定五节：结论 / 调用链 / 相关页面 / 源文件引用 / 不确定事项。
发现 Wiki 与源码不一致时记入「不确定事项」——s12 的 Fixer 会消费这些发现。

新增工具（其余全部沿用 s08，Agent Loop 仍然一个字没改）：
  - list_wiki_pages()        按类型分组列出全部页面（含 status/来源数/别名）
  - read_wiki_page(slug)     读整页；自动跟随 s04 合并留下的重定向
  - search_wiki(query)       标题/别名/正文的文本搜索（无向量，课程边界）

对照 WeKnora：
  - wiki-qa agent 类型（config/agent_type_presets.yaml:68）——预设了同样的
    「Wiki 优先」工具集与提示词策略；
  - 工具名映射：read_wiki_page ↔ wiki_read_page、search_wiki ↔ wiki_search
    （tools/definitions.go:25-31）；教学版的「read_file 核实引用」对应
    wiki_read_source_doc.go（用知识点 ID 精读原始文档——生产版引用单位是
    chunk/知识点，教学版是 文件:行号）；
  - chat_pipeline/wiki_boost.go：**流水线侧**的呼应——RAG 检索结果里命中
    Wiki 页面时加权，让蒸馏过的知识优先于原始切片。同一个「Wiki 优先」原则，
    在两个范式里各有一个实现，这就是双范式的互补。

用法：
    python code.py query "文档上传后经过哪些处理步骤？"
    python code.py query "Agent 引擎的循环是怎么工作的？" --mock
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# ─────────────────────────── 配置（同 s08 + WORKSPACE） ───────────────────────────

SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", r"C:\Desktop\Project\WeKnora")).resolve()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = Path(os.environ.get("WIKI_WORKSPACE", PROJECT_ROOT / "workspace")).resolve()
WIKI_DIR = WORKSPACE / "wiki"
STATE_DIR = WORKSPACE / "state"

MAX_ITERATIONS_DEFAULT = 20
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


# ─────────────────────────── 权限层（同 s08） ───────────────────────────

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
            raise PermissionDenied(
                f"读取被拒：{p} 不在允许的根目录内"
                f"（只读 {SOURCE_ROOT} 与 {PROJECT_ROOT}）。")
    else:
        if not _is_within(p, PROJECT_ROOT):
            raise PermissionDenied(
                f"写入被拒：{p} 不在 {PROJECT_ROOT} 内。"
                "被分析项目（SOURCE_ROOT）是只读的。")
    return p


# ─────────────────────────── 工具注册表（同 s08） ───────────────────────────

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


def execute_tool(name: str, args: dict) -> str:
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


# ─────────────────────────── 源码工具（同 s08） ───────────────────────────

@tool("bash",
      "在 WeKnora 根目录执行 bash 命令。逃生舱：优先用原子工具。",
      {"type": "object",
       "properties": {"command": {"type": "string"}}, "required": ["command"]})
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


@tool("read_file",
      "读文件，带行号。默认前 200 行；大文件用 offset/limit 分段。"
      "相对路径相对 WeKnora 根；Wiki 页面引用的源码用这个工具核实。",
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


# ─────────────────────────── 新增：Wiki 导航工具 ───────────────────────────

def _pages_meta() -> dict:
    return load_json(STATE_DIR / "pages.json", {"pages": {}, "redirects": {}})


def _find_page_file(slug: str):
    for sub in PAGE_TYPE_DIRS:
        p = WIKI_DIR / sub / f"{slug}.md"
        if p.is_file():
            return p
    return None


@tool("list_wiki_pages",
      "列出 Wiki 全部页面（按类型分组），含标题、状态、来源数、别名。"
      "回答任何问题前先看这里有什么可用。",
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
            status = f"，{m['status']}" if m.get("status") else ""
            lines.append(f"  {slug} — {m.get('title', slug)}"
                         f"（来源 {len(m.get('sources', []))} 个{status}{alias}）")
    idx = WIKI_DIR / "index.md"
    if idx.is_file():
        lines.append("[index] wiki/index.md（总目录，read_wiki_page 用 slug=index）")
    return "\n".join(lines) or "（Wiki 为空——先运行流水线篇的 ingest）"


@tool("read_wiki_page",
      "读取一个 Wiki 页面全文（含 frontmatter 的来源引用）。自动跟随合并重定向。",
      {"type": "object",
       "properties": {"slug": {"type": "string", "description": "页面 slug，如 internal-agent；index 读总目录"}},
       "required": ["slug"]})
def tool_read_wiki_page(slug: str) -> str:
    slug = slug.strip().lower()
    if slug == "index":
        idx = WIKI_DIR / "index.md"
        return idx.read_text(encoding="utf-8") if idx.is_file() else "[错误] index.md 不存在"
    prefix = ""
    redirects = _pages_meta().get("redirects", {})
    if slug in redirects:                      # s04 合并留下的转发
        target = redirects[slug]
        prefix = f"[重定向] {slug} 已被合并进 {target}，以下是 {target} 的内容：\n\n"
        slug = target
    p = _find_page_file(slug)
    if p is None:
        return (f"[错误] 页面不存在：{slug}。用 list_wiki_pages 或 search_wiki 找正确的 slug。")
    return prefix + p.read_text(encoding="utf-8")


@tool("search_wiki",
      "在 Wiki 的标题/别名/正文里做文本搜索（大小写不敏感），返回命中页面与摘录。"
      "这是查 Wiki 的第一入口。",
      {"type": "object",
       "properties": {"query": {"type": "string", "description": "关键词或短语"}},
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
            for lineno, line in enumerate(
                    p.read_text(encoding="utf-8").splitlines(), 1):
                if q in line.casefold():
                    body_hits.append(f"    L{lineno}: {line.strip()[:150]}")
                if len(body_hits) >= 3:
                    break
            if hit_title or hit_alias or body_hits:
                tag = "标题" if hit_title else ("别名" if hit_alias else "正文")
                results.append(f"{slug} — {title}（命中: {tag}）")
                results.extend(body_hits[:3])
            if len(results) >= WIKI_SEARCH_MAX:
                results.append("...[结果过多，请换更具体的关键词]")
                return "\n".join(results)
    return "\n".join(results) or f"（Wiki 中没有「{query}」的匹配——可用 grep 搜源码兜底）"


# ─────────────────────────── System Prompt（问答策略） ───────────────────────────

SYSTEM_PROMPT = f"""你是 WeKnora 项目的 Wiki 问答 Agent。
被分析项目（只读）：{SOURCE_ROOT}
Wiki 位置：{WIKI_DIR}（由流水线从源码蒸馏生成，页面带 `路径:行号` 来源引用）

## 回答优先级（严格遵守）
1. **先查 Wiki**：search_wiki / list_wiki_pages 找到相关页面，read_wiki_page 精读。
   Wiki 是预蒸馏的知识，一页顶几十次 grep。
2. **核实引用**：对回答起支撑作用的关键事实，用 read_file 打开页面引用的
   源文件行号核实——Wiki 可能过时，源码是最终事实。
3. **搜索兜底**：Wiki 覆盖不到的部分，再用 grep / glob / list_files 搜源码补充。

## 回答结构（五节，用这些标题）
## 结论 —— 直接回答问题
## 调用链 —— 涉及的函数/模块调用关系（能给出 A → B → C 的形式最好）
## 相关页面 —— 引用了哪些 Wiki 页面（slug）
## 源文件引用 —— 关键事实的 文件路径:行号 清单
## 不确定事项 —— 没核实的推测；**发现 Wiki 与源码不一致也记在这里**

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


class MockLLM:
    """脚本化的三级瀑布（工具真实执行）：
      1 search_wiki（命中别名）→ 2 read_wiki_page（含一次重定向跟随）
      → 3 read_file 核实页面引用 → 4 grep 补充 → 5 五节结构收敛"""

    def __init__(self):
        self.round = 0

    def chat(self, messages: list, tools: list):
        self.round += 1
        script = {
            1: ("第 1 级：先搜 Wiki。",
                [("search_wiki", {"query": "Agent ReAct 引擎"})]),
            2: ("命中 internal-agent（经由别名）。读整页；顺便验证旧 slug 的重定向。",
                [("read_wiki_page", {"slug": "internal-agent-engine"})]),
            3: ("第 2 级：页面引用了 engine.go:349-433，打开核实。",
                [("read_file", {"path": "internal/agent/engine.go",
                                "offset": 384, "limit": 12})]),
            4: ("第 3 级：页面没提 handleMaxIterations 的位置，grep 补充。",
                [("grep", {"pattern": "func .*handleMaxIterations",
                           "path": "internal/agent", "include": "*.go"})]),
        }
        if self.round in script:
            text, calls = script[self.round]
            return self._msg(text, calls)
        return self._msg(
            "## 结论\n（mock）Agent 引擎的循环在 executeLoop：以 state.CurrentRound < "
            "MaxIterations 为边界，每轮 runReActIteration 执行 think → act → observe，"
            "模型不再发起工具调用即收敛；迭代耗尽由 handleMaxIterations 兜底。\n\n"
            "## 调用链\nexecuteLoop → runReActIteration → (think → act → observe) "
            "→ finalize / handleMaxIterations\n\n"
            "## 相关页面\ninternal-agent（经由别名「Agent ReAct 引擎」命中；"
            "旧 slug internal-agent-engine 已重定向）\n\n"
            "## 源文件引用\n- internal/agent/engine.go:386（循环边界，第 3 轮核实）\n"
            "- internal/agent/finalize.go:160（handleMaxIterations，第 4 轮 grep 命中）\n\n"
            "## 不确定事项\n- mock 只核实了循环边界一处；真实 LLM 应对每条关键事实做"
            "第 2 级核实，并把 Wiki 与源码的不一致记录在此。", [])

    def _msg(self, text, calls):
        tool_calls = [SimpleNamespace(
            id=f"call_{self.round}_{i}", type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            for i, (name, args) in enumerate(calls)]
        return SimpleNamespace(content=text, tool_calls=tool_calls or None)


# ─────────────────────────── Agent Loop（与 s07/s08 完全相同） ───────────────────────────

def agent_loop(question: str, llm, max_iterations: int) -> str:
    tools = [entry["schema"] for entry in TOOL_REGISTRY.values()]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    for round_no in range(1, max_iterations + 1):
        print(f"\n───── 第 {round_no}/{max_iterations} 轮 ─────")
        msg = llm.chat(messages, tools)
        if msg.content:
            print(f"[模型] {msg.content}")
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            **({"tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls]} if msg.tool_calls else {}),
        })
        if not msg.tool_calls:
            return msg.content or "（模型没有给出内容）"
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            print(f"[工具] {tc.function.name}({json.dumps(args, ensure_ascii=False)[:120]})")
            result = execute_tool(tc.function.name, args)
            preview = result if len(result) <= 300 else result[:300] + " ..."
            print(f"[输出] {preview}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return f"[护栏] 达到最大迭代数 {max_iterations}，强制停止。"


def main():
    parser = argparse.ArgumentParser(description="s09：Wiki 问答 Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("query", help="基于 Wiki + 源码回答问题")
    p.add_argument("question")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS_DEFAULT)
    args = parser.parse_args()

    print(f"已注册工具：{', '.join(TOOL_REGISTRY)}")
    llm = MockLLM() if args.mock else OpenAILLM(load_env())
    answer = agent_loop(args.question, llm, args.max_iterations)
    print(f"\n═════ 最终回答 ═════\n{answer}")


if __name__ == "__main__":
    main()
