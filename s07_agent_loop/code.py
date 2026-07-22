# -*- coding: utf-8 -*-
"""
s07：最小 Agent Loop —— 「模型即决策者」

Agent 篇开篇。整个 Agent 的核心就是这个循环：

    while True:
        response = call_llm(system_prompt, messages, tools)
        messages.append(response)
        if response 不含 tool calls:
            return response.text            # 模型认为答完了 → 循环结束
        messages.append(execute_tool_calls(response.tool_calls))

与 s01「LLM 即函数」的本质区别只有一个：**谁掌握控制流**。
  - s01：代码决定「读哪个文件、调几次 LLM、失败怎么办」，模型只做一步转换；
  - s07：模型决定「执行什么命令、要不要继续探索、什么时候给出答案」，
    代码（Harness）只负责忠实执行工具调用并把结果喂回去。

本阶段只有一个工具：bash。没有任何针对任务的硬编码逻辑——模型怎么查
「WeKnora 用了哪些技术栈」完全由它自己规划。唯一的护栏：
  - MAX_ITERATIONS（对照 engine.go 的 MaxIterations，:386 的循环边界）；
  - 命令黑名单 + 敏感文件红线（完整权限层在 s08）；
  - 工具输出截断（防止一条 cat 撑爆上下文）。

对照 WeKnora（internal/agent/engine.go）：
  - executeLoop（:351）就是上面的 while：`for state.CurrentRound < e.config.MaxIterations`；
  - runReActIteration（:457）是单轮的 think → analyze → act → observe；
  - iterOutcome 哨兵（:435）决定循环 continue/break——教学版用「有无 tool_calls」表达；
  - handleMaxIterations（finalize.go:160）在迭代耗尽时兜底收尾，保证一定有输出；
  - 生产版在这层骨架外还包了：token 估算与上下文预算、memory consolidator
    （agent/memory/）、Langfuse span 追踪、事件总线流式输出——教学版全部砍掉，
    只留骨架。骨架不变，这正是「后续只加工具与状态，不重写循环」的底气。

对照参考项目：learn-claude-code v0（纯 bash agent）/ v1（基础工具）。
注意它用 Anthropic SDK（tool_use/tool_result 内容块），本项目用 OpenAI 兼容
API（message.tool_calls + role:"tool" 消息），消息结构差异见根 README 对照表。

用法：
    python code.py "WeKnora 用了哪些技术栈？各顶层目录职责是什么？"
    python code.py --mock                     # 无 API Key 演示循环机制（真实执行 bash）
    python code.py --mock-loop --max-iterations 3   # 演示 MaxIterations 护栏
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# ─────────────────────────── 配置 ───────────────────────────

SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", r"C:\Desktop\Project\WeKnora"))
MAX_ITERATIONS_DEFAULT = 15     # 对照 e.config.MaxIterations
MAX_TOOL_OUTPUT = 4000          # 单条工具输出的截断上限（护上下文，s11 会系统化）
BASH_TIMEOUT = 30               # 秒

# 安全红线（s01 起生效；s08 会升级为完整权限层）
SENSITIVE_RE = re.compile(r"\.env\b|\.pem\b|\.key\b|credentials|secret|id_rsa", re.I)
DESTRUCTIVE_PATTERNS = ["rm -rf", "rm -r /", "mkfs", "shutdown", "reboot",
                        "git push", "git commit", "> /", "format "]


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


# ─────────────────────────── 唯一的工具：bash ───────────────────────────

TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": "在被分析项目的根目录下执行一条 bash 命令并返回输出（stdout+stderr）。"
                       "用于探索代码库：ls、cat、head、grep、find、wc 等。只读操作。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 bash 命令"},
            },
            "required": ["command"],
        },
    },
}]


def find_bash() -> str:
    """定位可用的 bash。Windows 陷阱：PATH 里的 bash 可能是 System32 的 WSL 存根
    （未装 WSL 时直接报错），所以优先找 Git Bash 的真实安装路径。"""
    import shutil
    git = shutil.which("git")
    candidates = []
    if git:      # 从 git.exe 位置推导：C:\...\Git\cmd\git.exe → C:\...\Git\bin\bash.exe
        git_root = Path(git).parent.parent
        candidates += [git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    which = shutil.which("bash")
    if which and "system32" not in which.lower():   # 避开 WSL 存根
        return which
    return ""


BASH_EXE = find_bash()


def run_bash(command: str) -> str:
    """执行 bash 命令。Harness 的职责：忠实执行 + 守住红线 + 截断输出。
    拒绝时返回说明文字（不抛异常）——模型看到拒绝理由后会自己调整策略，
    这正是 Agent 范式的自愈能力。"""
    lowered = command.lower()
    if SENSITIVE_RE.search(lowered):
        return "[拒绝] 命令涉及敏感文件（.env / 密钥 / 凭据），安全红线禁止访问。"
    for pat in DESTRUCTIVE_PATTERNS:
        if pat in lowered:
            return f"[拒绝] 命令包含黑名单模式「{pat}」。本 Agent 对项目只读。"
    if not BASH_EXE:
        return "[错误] 找不到 bash。Windows 下请安装 Git for Windows。"
    try:
        result = subprocess.run(
            [BASH_EXE, "-c", command], cwd=SOURCE_ROOT,
            capture_output=True, text=True, timeout=BASH_TIMEOUT,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return f"[错误] 命令超时（>{BASH_TIMEOUT}s）。请换更精确的命令。"
    output = (result.stdout + result.stderr).strip() or "（无输出）"
    if len(output) > MAX_TOOL_OUTPUT:
        output = output[:MAX_TOOL_OUTPUT] + f"\n...[截断，原始输出 {len(output)} 字符]"
    return output


# ─────────────────────────── System Prompt（含安全红线） ───────────────────────────

SYSTEM_PROMPT = f"""你是一个代码库分析 Agent。工作目录是 WeKnora 项目根目录：{SOURCE_ROOT}

你只有一个工具：bash。用它自主探索代码库来回答用户的问题——
先看目录结构，再按需读关键文件（go.mod / package.json / CLAUDE.md / README 等），
证据足够了就停下来作答，不要无目的地遍历。

## 安全红线（不可违反）
- 严禁读取任何敏感文件：.env、.env.*、*.pem、*.key、credentials*、secret*、id_rsa*；
- 对项目只读：严禁任何写入、删除、移动、git 提交/推送操作；
- 这些红线由执行层强制（违规命令会被拒绝），但你应当从一开始就不发起违规命令。

## 回答要求
- 用中文回答，给出结论 + 依据（引用你实际看过的文件路径）；
- 不确定的内容明确说不确定，不要编造。"""


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
    """脚本化的「决策序列」，演示循环机制（工具是真实执行的，只有决策是预演的）：
      第 1 轮：ls 看顶层结构
      第 2 轮：并行三个调用——读 go.mod、读 CLAUDE.md 开头、以及一次会被
              红线拒绝的 cat .env（演示：Harness 拒绝 → 模型看到拒绝理由）
      第 3 轮：无工具调用 → 循环终止
    loop_forever=True 时永远发起工具调用，用于演示 MaxIterations 护栏。"""

    def __init__(self, loop_forever: bool = False):
        self.round = 0
        self.loop_forever = loop_forever

    def chat(self, messages: list, tools: list):
        self.round += 1
        if self.loop_forever:
            return self._msg("我还想再看看……（mock 永不收敛）",
                             [("bash", {"command": "echo 第%d轮探索" % self.round})])
        if self.round == 1:
            return self._msg("先看顶层目录结构。", [("bash", {"command": "ls"})])
        if self.round == 2:
            return self._msg("读 go.mod 和 CLAUDE.md 确认技术栈；顺便试试读 .env（会被拒绝）。",
                             [("bash", {"command": "head -n 15 go.mod"}),
                              ("bash", {"command": "head -n 30 CLAUDE.md"}),
                              ("bash", {"command": "cat .env"})])
        return self._msg(
            "（mock 最终回答）基于上面真实执行的探索：顶层有 cmd/、internal/、docreader/、"
            "frontend/、cli/ 等目录；go.mod 表明这是 Go 后端，CLAUDE.md 说明了 "
            "Go 服务 + Python docreader + Vue 前端的多服务结构。注意第 2 轮里 "
            "cat .env 被执行层拒绝了——红线在 Harness，不靠模型自觉。"
            "真实 LLM 会在此基础上给出完整的技术栈与目录职责分析。", [])

    def _msg(self, text, calls):
        tool_calls = [SimpleNamespace(
            id=f"call_{self.round}_{i}", type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            for i, (name, args) in enumerate(calls)]
        return SimpleNamespace(content=text, tool_calls=tool_calls or None)


# ─────────────────────────── Agent Loop（本阶段的全部核心） ───────────────────────────

def agent_loop(question: str, llm, max_iterations: int) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    for round_no in range(1, max_iterations + 1):
        print(f"\n───── 第 {round_no}/{max_iterations} 轮 ─────")
        msg = llm.chat(messages, TOOLS)

        if msg.content:
            print(f"[模型] {msg.content}")

        # 把 assistant 消息（含 tool_calls）原样放回历史——模型下一轮要看到自己说过什么
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            **({"tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls]} if msg.tool_calls else {}),
        })

        if not msg.tool_calls:
            return msg.content or "（模型没有给出内容）"   # 模型认为答完了

        # 执行每个工具调用，结果以 role:"tool" 消息喂回
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            command = args.get("command", "")
            print(f"[工具] bash$ {command}")
            result = run_bash(command)
            preview = result if len(result) <= 300 else result[:300] + " ..."
            print(f"[输出] {preview}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # 对照 handleMaxIterations（finalize.go:160）：迭代耗尽也要有交代
    return (f"[护栏] 达到最大迭代数 {max_iterations}，强制停止。"
            "已收集的信息在对话历史中，但模型未能收敛出最终答案。"
            "可以提高 --max-iterations 或把问题拆小。")


# ─────────────────────────── 入口 ───────────────────────────

def main():
    parser = argparse.ArgumentParser(description="s07：最小 Agent Loop（bash 单工具）")
    parser.add_argument("question", nargs="?",
                        default="WeKnora 用了哪些技术栈？各顶层目录职责是什么？")
    parser.add_argument("--mock", action="store_true",
                        help="脚本化决策演示循环机制（bash 真实执行）")
    parser.add_argument("--mock-loop", action="store_true",
                        help="mock 永不收敛，演示 MaxIterations 护栏")
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS_DEFAULT)
    args = parser.parse_args()

    if args.mock_loop:
        llm = MockLLM(loop_forever=True)
    elif args.mock:
        llm = MockLLM()
    else:
        llm = OpenAILLM(load_env())

    answer = agent_loop(args.question, llm, args.max_iterations)
    print(f"\n═════ 最终回答 ═════\n{answer}")


if __name__ == "__main__":
    main()
