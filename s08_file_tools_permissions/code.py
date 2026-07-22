# -*- coding: utf-8 -*-
"""
s08：原子文件工具与权限边界

s07 只有一个粗粒度的 bash：模型要自己拼 grep/find 命令，红线靠命令文本匹配
（`cat .e*v` 就能绕过）。s08 做两件事：

1. **原子文件工具 + 统一注册表**：list_files / read_file / grep / glob / write_file。
   一个工具一个职责；新增工具 = schema + handler + 注册（@tool 装饰器），
   循环代码零改动——s07 的承诺兑现。

2. **权限层**：所有文件访问的必经之路 `resolve_path()`：
     - 路径规范化（resolve 展开 ..、符号链接；Windows 大小写不敏感比较）；
     - 读：只允许 SOURCE_ROOT（WeKnora，只读）和 PROJECT_ROOT（llm-wiki-learning）；
     - 写：只允许 PROJECT_ROOT 内部——SOURCE_ROOT 一个字节都不许改；
     - 敏感文件（.env/*.pem/credentials…）在**规范化之后**检查文件名，连读都拒绝：
       `internal/agent/../../.env` 这类穿越写法逃不掉。
   bash 保留 s07 的黑名单 + 超时，作为「粗粒度逃生舱」存在（见 README 讨论）。

Agent Loop 与 s07 完全相同（一个字没改）——只是工具执行从 if/else 换成查注册表。

对照 WeKnora：
  - tools/registry.go：ToolRegistry.RegisterTool（:47）——同样的「注册表 + 按名分发」；
  - tools/definitions.go：全部工具名的集中定义（ToolKnowledgeSearch、ToolWikiWritePage…）；
  - tools/scope_authorization.go：WeKnora 的「权限层」管的是**数据范围**
    （searchTargetsAllowKnowledgeID：这个 Agent 允许碰哪些知识库/文档/标签），
    教学版管的是**文件系统范围**——层的位置一样：工具执行的必经之路上。

用法：
    python code.py "executeLoop 定义在哪个文件？循环的推进条件是什么？"
    python code.py --mock        # 无 API Key 演示全部工具与权限拒绝
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

# ─────────────────────────── 配置 ───────────────────────────

SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", r"C:\Desktop\Project\WeKnora")).resolve()
PROJECT_ROOT = Path(__file__).resolve().parent.parent          # llm-wiki-learning/
MAX_ITERATIONS_DEFAULT = 15
MAX_TOOL_OUTPUT = 4000
BASH_TIMEOUT = 30
READ_DEFAULT_LIMIT = 200      # read_file 默认行数
READ_MAX_LIMIT = 500          # read_file 单次上限
GREP_MAX_RESULTS = 50
GLOB_MAX_RESULTS = 100
LIST_MAX_ENTRIES = 200

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


# ─────────────────────────── 权限层：所有文件访问的必经之路 ───────────────────────────

class PermissionDenied(Exception):
    pass


def _is_within(p: Path, root: Path) -> bool:
    """Windows 路径大小写不敏感，必须 casefold 后比较；
    加尾分隔符防止 C:\\foo 前缀误匹配 C:\\foobar。"""
    ps, rs = str(p).casefold(), str(root).casefold()
    return ps == rs or ps.startswith(rs.rstrip("\\/") + os.sep)


def resolve_path(raw: str, mode: str) -> Path:
    """规范化 + 授权。mode = "read" | "write"。
    相对路径的落点：读 → SOURCE_ROOT；写 → PROJECT_ROOT（各自的默认语境）。
    返回规范化后的绝对路径；越权抛 PermissionDenied（消息会喂回给模型）。"""
    raw = raw.strip().strip('"')
    p = Path(raw)
    if not p.is_absolute():
        p = (SOURCE_ROOT / p) if mode == "read" else (PROJECT_ROOT / p)
    p = p.resolve()          # 关键：展开 .. 与符号链接，再做后续所有检查

    # 敏感文件红线：规范化之后按最终文件名判，穿越写法逃不掉
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
                "被分析项目（SOURCE_ROOT）是只读的，一个字节都不许改。")
    return p


# ─────────────────────────── 工具注册表 ───────────────────────────
# 对照 tools/registry.go 的 RegisterTool + definitions.go 的集中定义。
# 新增工具 = 写一个带 @tool 的函数，循环与分发代码零改动。

TOOL_REGISTRY = {}   # name -> {"schema": OpenAI tool 定义, "handler": 函数}


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
        return f"[拒绝] {e}"                # 拒绝是信息，不是异常（s07 的原则）
    except TypeError as e:
        return f"[错误] 参数不合法：{e}"
    except Exception as e:                  # 工具内部错误也不能炸掉循环
        return f"[错误] {type(e).__name__}: {e}"
    if len(result) > MAX_TOOL_OUTPUT:
        result = result[:MAX_TOOL_OUTPUT] + f"\n...[截断，原始输出 {len(result)} 字符]"
    return result


# ─────────────────────────── 工具实现 ───────────────────────────

@tool("bash",
      "在 WeKnora 根目录执行一条 bash 命令（stdout+stderr）。粗粒度逃生舱：优先用"
      "原子工具（list_files/read_file/grep/glob），只有它们覆盖不了时才用 bash。",
      {"type": "object",
       "properties": {"command": {"type": "string", "description": "bash 命令"}},
       "required": ["command"]})
def tool_bash(command: str) -> str:
    lowered = command.lower()
    if SENSITIVE_RE.search(lowered):
        return "[拒绝] 命令涉及敏感文件，安全红线禁止访问。"
    for pat in DESTRUCTIVE_PATTERNS:
        if pat in lowered:
            return f"[拒绝] 命令包含黑名单模式「{pat}」。对项目只读。"
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


@tool("list_files",
      "列出一个目录的内容（一层，不递归）。目录名以 / 结尾。相对路径相对 WeKnora 根。",
      {"type": "object",
       "properties": {"path": {"type": "string", "description": "目录路径，默认 ."}},
       "required": []})
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
            continue                       # 敏感文件连名字都不展示
        entries.append(child.name + "/" if child.is_dir()
                       else f"{child.name}  ({child.stat().st_size:,} B)")
        if len(entries) >= LIST_MAX_ENTRIES:
            entries.append(f"...[已达 {LIST_MAX_ENTRIES} 条上限]")
            break
    return "\n".join(entries) or "（空目录）"


@tool("read_file",
      "读取文件内容，带行号（行号可直接用于 Wiki 引用）。默认前 200 行；"
      "大文件用 offset/limit 分段读。相对路径相对 WeKnora 根。",
      {"type": "object",
       "properties": {
           "path": {"type": "string"},
           "offset": {"type": "integer", "description": "起始行号（1-based），默认 1"},
           "limit": {"type": "integer", "description": f"读多少行，默认 {READ_DEFAULT_LIMIT}，上限 {READ_MAX_LIMIT}"}},
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


@tool("grep",
      "在目录树里正则搜索文件内容，返回 文件:行号:内容。自动跳过排除目录与敏感文件。"
      "相对路径相对 WeKnora 根。",
      {"type": "object",
       "properties": {
           "pattern": {"type": "string", "description": "正则表达式"},
           "path": {"type": "string", "description": "搜索目录，默认 ."},
           "include": {"type": "string", "description": "文件名过滤，如 *.go"}},
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
                        hits.append(f"...[已达 {GREP_MAX_RESULTS} 条上限，请缩小范围]")
                        return "\n".join(hits)
    return "\n".join(hits) or "（无匹配）"


@tool("glob",
      "按通配符找文件（如 **/*.go、internal/agent/*.go）。相对路径相对 WeKnora 根。",
      {"type": "object",
       "properties": {
           "pattern": {"type": "string", "description": "通配符模式"},
           "path": {"type": "string", "description": "起始目录，默认 ."}},
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


@tool("write_file",
      "写入文件（覆盖）。只允许写 llm-wiki-learning 内部（workspace/ 等）；"
      "被分析项目只读。相对路径相对 llm-wiki-learning 根。",
      {"type": "object",
       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
       "required": ["path", "content"]})
def tool_write_file(path: str, content: str) -> str:
    p = resolve_path(path, "write")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    return f"[写入] {p}（{len(content)} 字符）"


# ─────────────────────────── System Prompt ───────────────────────────

SYSTEM_PROMPT = f"""你是一个代码库分析 Agent。被分析项目（只读）：{SOURCE_ROOT}
你的可写区域：{PROJECT_ROOT} 内部（如 workspace/）。

工具使用策略：
- 定位文件用 glob / list_files，找符号用 grep，精读用 read_file（带行号，便于引用）；
- bash 只在原子工具覆盖不了时用（如 wc、管道统计）；
- 大文件用 read_file 的 offset/limit 分段读，不要一次吞下。

## 安全红线（由权限层强制）
- 敏感文件（.env、*.pem、credentials* 等）连读都不允许；
- 被分析项目严格只读；写操作只允许在你的可写区域内。

## 回答要求
- 中文；结论 + 依据（引用 文件路径:行号）；不确定的明说。"""


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
    """脚本化决策，覆盖全部工具 + 三类权限拒绝（工具真实执行）：
      1 glob 定位 engine 文件 → 2 grep 找 executeLoop → 3 read_file 精读定义处
      4 越权三连：读根外文件 / 穿越读 .env / 写 SOURCE_ROOT —— 全部被拒
      5 write_file 往 workspace 写笔记（允许）→ 6 收敛作答"""

    def __init__(self):
        self.round = 0

    def chat(self, messages: list, tools: list):
        self.round += 1
        script = {
            1: ("先定位 agent 相关文件。",
                [("glob", {"pattern": "internal/agent/*.go"})]),
            2: ("找 executeLoop 的定义位置。",
                [("grep", {"pattern": r"func .*executeLoop",
                           "path": "internal/agent", "include": "*.go"})]),
            3: ("精读 executeLoop 的循环边界。",
                [("read_file", {"path": "internal/agent/engine.go",
                                "offset": 349, "limit": 45})]),
            4: ("（mock 越权测试）试读根外文件、穿越读 .env、写被分析项目。",
                [("read_file", {"path": r"C:\Windows\win.ini"}),
                 ("read_file", {"path": r"internal/agent/../../.env"}),
                 ("write_file", {"path": r"C:\Desktop\Project\WeKnora\hack.txt",
                                 "content": "should be denied"})]),
            5: ("把发现写进 workspace 笔记（允许的写入）。",
                [("write_file", {"path": "workspace/logs/s08-scratch.md",
                                 "content": "# s08 探索笔记（mock）\n\n"
                                            "executeLoop: internal/agent/engine.go:351\n"
                                            "循环边界: state.CurrentRound < e.config.MaxIterations（:386）\n"})]),
        }
        if self.round in script:
            text, calls = script[self.round]
            return self._msg(text, calls)
        return self._msg(
            "（mock 最终回答）executeLoop 定义在 internal/agent/engine.go:351，"
            "循环推进条件是 state.CurrentRound < e.config.MaxIterations（engine.go:386，"
            "见第 3 轮 read_file 的真实输出）。第 4 轮三次越权全部被权限层拒绝："
            "根外读取、.. 穿越读 .env、写 SOURCE_ROOT——路径在规范化之后检查，"
            "穿越写法逃不掉。第 5 轮向 workspace 写入成功：写权限只对内。", [])

    def _msg(self, text, calls):
        tool_calls = [SimpleNamespace(
            id=f"call_{self.round}_{i}", type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            for i, (name, args) in enumerate(calls)]
        return SimpleNamespace(content=text, tool_calls=tool_calls or None)


# ─────────────────────────── Agent Loop（与 s07 完全相同） ───────────────────────────

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
    return (f"[护栏] 达到最大迭代数 {max_iterations}，强制停止。")


def main():
    parser = argparse.ArgumentParser(description="s08：原子文件工具与权限边界")
    parser.add_argument("question", nargs="?",
                        default="executeLoop 定义在哪个文件？循环的推进条件是什么？")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS_DEFAULT)
    args = parser.parse_args()

    print(f"已注册工具：{', '.join(TOOL_REGISTRY)}")
    llm = MockLLM() if args.mock else OpenAILLM(load_env())
    answer = agent_loop(args.question, llm, args.max_iterations)
    print(f"\n═════ 最终回答 ═════\n{answer}")


if __name__ == "__main__":
    main()
