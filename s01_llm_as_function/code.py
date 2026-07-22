# -*- coding: utf-8 -*-
"""
s01：结构化 LLM 调用 —— 「LLM 即函数」

本阶段没有循环、没有工具。只做一件事：

    源文件内容 ──模板 prompt──▶ LLM ──▶ JSON ──schema 校验──▶ 结构化摘要
                                          │ 校验失败
                                          ▼
                                   带错误信息重试（最多 2 次）

这就是流水线的基石：把 LLM 当成一个「输入文本、输出结构化数据」的函数。
控制流（读文件、截断、校验、重试）全部由代码决定，模型只负责这一步转换。

对照 WeKnora：
  - generateWithTemplate（internal/application/service/wiki_ingest.go）
  - maxContentForWiki = 32768 截断（同文件）
  - RepairJSON（internal/agent/tools/json_repair.go）——教学版只做「剥围栏 + 取花括号」
  - prompts_wiki.go 的 JSON Formatting Rules（本文件的 prompt 借鉴其写法）

用法：
    python code.py <源文件路径>            # 真实 LLM（读 .env）
    python code.py <源文件路径> --mock     # Mock LLM，无需 API Key，演示失败重试
"""

import argparse
import fnmatch
import json
import sys
from pathlib import Path

# ─────────────────────────── 配置常量 ───────────────────────────

# 对照 WeKnora 的 maxContentForWiki = 32768：
# 送给 LLM 的内容有硬上限，超出即截断。这是流水线「成本可控」原则的最直接体现——
# 没有上限的输入意味着没有上限的账单。
MAX_CONTENT_CHARS = 32768

# schema 校验失败后的最大重试次数（不含第一次调用）
MAX_RETRIES = 2

# ─────────────────────────── 安全红线 ───────────────────────────
# WeKnora 根目录存在真实 .env（含 API 密钥）。任何阶段都不得把敏感文件内容
# 发送给 LLM。s01 虽然只处理用户指定的单个文件，排除清单也从现在就生效。
SENSITIVE_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "credentials*", "secret*", "*.secret", "id_rsa*",
]


def is_sensitive_path(path: Path) -> bool:
    """文件名匹配任一敏感模式即拒绝（大小写不敏感）。"""
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_PATTERNS)


# ─────────────────────────── .env 加载 ───────────────────────────
# .env 只是 KEY=VALUE 的纯文本。手写十几行加载器，省掉一个依赖，
# 也让你看清「配置是怎么进来的」。查找顺序：本阶段目录 → 项目根目录。

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


# ─────────────────────────── Prompt 模板 ───────────────────────────
# 对照 prompts_wiki.go：明确的字段定义 + 严格的 JSON Formatting Rules。
# 「字符串值内不许有裸换行」这条规则直接来自 WeKnora——这是 LLM 产出非法 JSON
# 的头号原因。

SYSTEM_PROMPT = """你是一个代码分析器。给定一个源文件，你输出结构化的 JSON 摘要。

### JSON Formatting Rules
- 只输出 JSON 对象本身，不要任何前言、解释或 Markdown 代码围栏。
- 字符串值内不要使用裸换行符；确需换行用 \\n 转义。
- 所有字段都必须出现，即使为空数组。"""

EXTRACT_PROMPT = """分析下面这个源文件，返回 JSON 对象，字段定义：

- "summary": 字符串。这个文件做什么、在系统中扮演什么角色，3-5 句话，中文。
- "key_symbols": 字符串数组。文件中最重要的函数/类型/常量名（原样保留标识符），5-15 个。
- "responsibilities": 字符串数组。该文件承担的核心职责，每条一句话，中文，2-6 条。
- "references": 字符串数组。该文件引用的同项目内其他包/模块路径（从 import 推断），可为空。

文件路径：{path}

文件内容（可能已截断）：
<file_content>
{content}
</file_content>"""


# ─────────────────────────── JSON 提取与修复 ───────────────────────────
# 对照 internal/agent/tools/json_repair.go。生产版处理十几种畸形；
# 教学版只处理最常见的两种：Markdown 代码围栏、JSON 前后的多余文字。

def extract_json_text(raw: str) -> str:
    """从 LLM 返回的原始文本里剥出最可能是 JSON 的部分。"""
    text = raw.strip()
    # 情况 1：模型无视指令包了 ```json ... ``` 围栏
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    # 情况 2：JSON 前后有解释性文字 —— 取第一个 { 到最后一个 } 之间
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return text.strip()


# ─────────────────────────── Schema 校验 ───────────────────────────
# 手写校验而非 JSON Schema 库：规则一眼可见，错误信息直接喂回给模型。
# 返回错误列表；空列表 = 通过。

def validate_extraction(data) -> list:
    errors = []
    if not isinstance(data, dict):
        return ["顶层必须是 JSON 对象"]

    def check_str_list(field, min_len):
        value = data.get(field)
        if not isinstance(value, list):
            errors.append(f'字段 "{field}" 缺失或不是数组')
        elif not all(isinstance(item, str) and item.strip() for item in value):
            errors.append(f'字段 "{field}" 的元素必须全部是非空字符串')
        elif len(value) < min_len:
            errors.append(f'字段 "{field}" 至少需要 {min_len} 个元素')

    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        errors.append('字段 "summary" 缺失或不是非空字符串')
    check_str_list("key_symbols", 1)
    check_str_list("responsibilities", 1)
    check_str_list("references", 0)
    return errors


# ─────────────────────────── LLM 客户端 ───────────────────────────

class OpenAILLM:
    """真实调用：OpenAI 兼容 API。complete(messages) -> 文本。"""

    def __init__(self, env: dict):
        from openai import OpenAI  # 延迟导入：--mock 模式不需要装 openai
        api_key = env.get("OPENAI_API_KEY")
        if not api_key or api_key.startswith("sk-your"):
            sys.exit("错误：未配置 OPENAI_API_KEY。复制 .env.example 为 .env 并填写，"
                     "或用 --mock 运行。")
        self.client = OpenAI(api_key=api_key,
                             base_url=env.get("OPENAI_BASE_URL") or None)
        self.model = env.get("MODEL_ID", "gpt-4o-mini")

    def complete(self, messages: list) -> str:
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=0)
        return resp.choices[0].message.content or ""


class MockLLM:
    """Mock：按脚本依次返回预设响应，用于无 API Key 演示「失败 → 重试」路径。

    第 1 次返回：包了 Markdown 围栏、且缺少 responsibilities 字段 → 校验失败；
    第 2 次返回：合法 JSON → 校验通过。
    """

    def __init__(self):
        self.calls = 0
        self.responses = [
            # 第 1 次：两个经典错误——围栏 + 缺字段
            """```json
{
  "summary": "engine.go 实现 WeKnora 的 ReAct Agent 引擎，驱动 think-act-observe 循环。",
  "key_symbols": ["AgentEngine", "executeLoop", "runReActIteration", "MaxIterations"]
}
```""",
            # 第 2 次：模型看到校验错误反馈后修正
            json.dumps({
                "summary": ("engine.go 实现 WeKnora 的 ReAct Agent 引擎。它以 executeLoop 为主循环，"
                            "每一轮调用 runReActIteration 执行 think → act → observe，"
                            "直到模型给出最终答案或达到 MaxIterations 上限。"
                            "它是「模型即决策者」范式在 WeKnora 中的核心实现。"),
                "key_symbols": ["AgentEngine", "executeLoop", "runReActIteration",
                                "iterOutcome", "MaxIterations", "handleMaxIterations"],
                "responsibilities": [
                    "驱动 ReAct 主循环并在每轮之间维护 Agent 状态",
                    "在达到最大迭代数时兜底收尾，保证一定有输出",
                    "把工具调用结果拼回对话历史供下一轮推理",
                ],
                "references": ["internal/agent/tools", "internal/types"],
            }, ensure_ascii=False),
        ]

    def complete(self, messages: list) -> str:
        self.calls += 1
        # 依次弹出脚本响应；超出脚本则重复最后一个
        idx = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[idx]


# ─────────────────────────── 核心：一次结构化提取 ───────────────────────────

def extract_structured(file_path: Path, llm) -> dict:
    """读文件 → 截断 → 调 LLM → 校验，失败则带错误信息重试。

    这就是「LLM 即函数」：整个控制流是这段普通 Python 代码，
    模型只在 llm.complete() 那一行被调用。
    """
    # 安全红线：敏感文件连读都不读
    if is_sensitive_path(file_path):
        sys.exit(f"拒绝：{file_path.name} 匹配敏感文件排除清单，不会读取或发送给 LLM。")
    if not file_path.is_file():
        sys.exit(f"错误：文件不存在：{file_path}")

    content = file_path.read_text(encoding="utf-8", errors="replace")
    original_len = len(content)
    if original_len > MAX_CONTENT_CHARS:  # 对照 maxContentForWiki 截断
        content = content[:MAX_CONTENT_CHARS]
        print(f"[截断] 内容 {original_len} 字符 > 上限 {MAX_CONTENT_CHARS}，已截断")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": EXTRACT_PROMPT.format(path=file_path, content=content)},
    ]

    for attempt in range(1 + MAX_RETRIES):
        print(f"[调用] 第 {attempt + 1} 次 LLM 调用 ...")
        raw = llm.complete(messages)

        # 解析：先剥围栏/杂质，再 json.loads
        try:
            data = json.loads(extract_json_text(raw))
        except json.JSONDecodeError as e:
            errors = [f"JSON 解析失败：{e}"]
        else:
            errors = validate_extraction(data)
            if not errors:
                print(f"[通过] schema 校验通过（第 {attempt + 1} 次尝试）")
                return data

        # 校验失败：把模型的原始输出和错误清单一起追加进对话，让它自己改。
        # 这是流水线版的「错误反馈」——不换 prompt、不换模型，只把错误说清楚。
        print(f"[失败] 第 {attempt + 1} 次尝试未通过：{'; '.join(errors)}")
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content":
                         "你上一次的输出未通过校验，错误如下：\n- "
                         + "\n- ".join(errors)
                         + "\n\n请重新输出完整的 JSON 对象，修正以上全部错误。"
                         "只输出 JSON，不要围栏，不要解释。"})

    sys.exit(f"错误：重试 {MAX_RETRIES} 次后仍未通过 schema 校验，放弃。"
             "（s03 中这类失败会进入 pending.json 等待重跑，而不是让整批中断）")


# ─────────────────────────── 入口 ───────────────────────────

def main():
    parser = argparse.ArgumentParser(description="s01：对单个源文件做一次结构化 LLM 提取")
    parser.add_argument("file", help="要分析的源文件路径")
    parser.add_argument("--mock", action="store_true",
                        help="使用 Mock LLM（无需 API Key，演示失败重试路径）")
    args = parser.parse_args()

    llm = MockLLM() if args.mock else OpenAILLM(load_env())
    result = extract_structured(Path(args.file).resolve(), llm)

    print("\n=== 结构化提取结果 ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
