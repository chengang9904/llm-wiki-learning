# -*- coding: utf-8 -*-
"""
s02：单文件 → Wiki 页面（map 步骤）

在 s01「LLM 即函数」之上新增三件事：

  1. 扫描器 scan_sources()：遍历 WeKnora，目录排除清单 + 敏感文件红线 + 扩展名白名单，
     产出候选文件列表（这是未来 manifest 的雏形）；
  2. 提取 schema 升级为「页面级」：title / page_type / 概述 / 核心职责 /
     关键实现（每条必须带行号来源）/ 调用关系 / 未确认事项；
     行号会和真实文件行数比对——引用超出文件范围 = 幻觉，打回重试；
  3. 渲染器 render_page()：把提取结果渲染成带 frontmatter 的 Markdown 页面，
     写入 workspace/wiki/，并在 workspace/logs/wiki-log.md 追加一条修改记录。

对照 WeKnora：
  - mapOneDocument（wiki_ingest_batch.go:1109）——本阶段就是它的教学版：
    取内容 → 截断 → 守卫 → LLM 提取 → 产出页面更新
  - extractEntitiesAndConceptsNoUpsert（wiki_ingest_batch.go:1554）——单次提取、不落库
  - hasSufficientTextContent（wiki_ingest.go:2750）——空内容守卫，防止对无文本文件幻觉
  - types/wiki_page.go —— PageType / Status / SourceRefs 的原型

用法：
    python code.py scan                    # 扫描 WeKnora，打印候选清单与排除统计
    python code.py map <源文件> [--mock]   # 提取并生成一个 Wiki 页面
"""

import argparse
import datetime
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

# ─────────────────────────── 路径与常量 ───────────────────────────

# 被分析的项目根目录（只读）。可用环境变量 SOURCE_ROOT 覆盖。
SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", r"C:\Desktop\Project\WeKnora"))

# Wiki 数据目录：与各阶段平级的共享 workspace/
WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"

MAX_CONTENT_CHARS = 32768   # 对照 maxContentForWiki
MAX_RETRIES = 2

# 内容里非空白字符少于这个数就拒绝提取——对照 hasSufficientTextContent：
# 没有实质内容还硬调 LLM，模型只会一本正经地编。
MIN_TEXT_CHARS = 50

# ─────────────────────────── 排除清单（安全红线 + 噪音过滤） ───────────────────────────

SENSITIVE_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "credentials*", "secret*", "*.secret", "id_rsa*",
]

# 目录排除：版本库、依赖、构建产物、数据目录。frontend/ 与 web/ 也排除——
# npm 生态文件量巨大且对后端架构 Wiki 噪音多，前端架构页面后续从 docs/ 提取。
EXCLUDED_DIRS = {
    ".git", ".github", ".idea", ".vscode",
    "node_modules", "frontend", "web", "miniprogram",
    ".local-data", "__pycache__", ".venv", "venv",
    "vendor", "dist", "build",
}

# 文件排除：测试与生成代码不进 Wiki（省成本，且它们描述的是「怎么验证」而非「是什么」）
EXCLUDED_FILE_PATTERNS = ["*_test.go", "*.pb.go", "*_pb2.py", "*_pb2_grpc.py", "*.min.js"]

# 扩展名白名单：只收对理解架构有用的文本文件
INCLUDE_EXTS = {".go", ".py", ".md", ".yaml", ".yml", ".proto"}


def is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_PATTERNS)


def scan_sources(root: Path):
    """遍历 root，返回 (候选文件列表, 排除统计)。os.walk 手动剪枝目录——
    在 dirnames 里原地删除被排除的目录，walk 就不会进入它们。"""
    included, stats = [], {"dir_pruned": 0, "sensitive": 0, "ext": 0, "pattern": 0}
    for dirpath, dirnames, filenames in os.walk(root):
        pruned = [d for d in dirnames if d in EXCLUDED_DIRS]
        stats["dir_pruned"] += len(pruned)
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if is_sensitive_path(fpath):
                stats["sensitive"] += 1
                continue
            if fpath.suffix.lower() not in INCLUDE_EXTS:
                stats["ext"] += 1
                continue
            if any(fnmatch.fnmatch(fname, pat) for pat in EXCLUDED_FILE_PATTERNS):
                stats["pattern"] += 1
                continue
            included.append(fpath)
    return included, stats


# ─────────────────────────── .env 加载（同 s01） ───────────────────────────

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


# ─────────────────────────── 页面类型 ───────────────────────────
# 课程自定的 8 类（对照 WeKnora 的 PageType：summary/entity/concept/...，
# 分类维度不同但机制相同：type 决定页面归入哪个目录、在索引里怎么分组）。

PAGE_TYPE_DIRS = {
    "architecture": "architecture",
    "module": "modules",
    "workflow": "workflows",
    "api": "api",
    "data": "data",
    "infrastructure": "infrastructure",
    "decision": "decisions",
    "glossary": "glossary",
}

# ─────────────────────────── Prompt 模板 ───────────────────────────

SYSTEM_PROMPT = """你是一个代码分析器，为软件项目的 Wiki 生成页面素材。

### JSON Formatting Rules
- 只输出 JSON 对象本身，不要任何前言、解释或 Markdown 代码围栏。
- 字符串值内不要使用裸换行符；确需换行用 \\n 转义。
- 所有字段都必须出现，即使为空数组。

### 引用纪律
- 「关键实现」的每一条都必须带 lines 行号来源，行号必须真实存在于给出的带行号内容中。
- 没有把握的事实不要写进正文字段，写进 uncertainties。"""

EXTRACT_PAGE_PROMPT = """分析下面这个源文件（每行行首标了行号），返回 JSON 对象，字段定义：

- "title": 字符串。这个页面的标题，中文，简短名词短语（如「Agent ReAct 引擎」）。
- "page_type": 字符串。必须是以下之一：architecture, module, workflow, api, data,
  infrastructure, decision, glossary。单个源码文件通常是 "module"。
- "summary": 字符串。概述：这个文件做什么、在系统中扮演什么角色，3-5 句话，中文。
- "responsibilities": 字符串数组。核心职责，每条一句话，中文，2-6 条。
- "key_implementation": 对象数组。关键实现要点，3-8 条，每条：
    {{"point": "一句话说明（可含标识符）", "lines": "起始行-结束行（如 349-433，单行可写 349）"}}
  lines 必须落在文件真实行号范围内。
- "call_relations": 字符串数组。调用关系：它 import/调用了什么、推测被谁使用，可为空。
- "uncertainties": 字符串数组。未确认事项：从这个文件本身无法确定、需要看其他文件
  才能证实的推测，可为空。

文件路径：{path}

文件内容（带行号，可能已截断）：
<file_content>
{content}
</file_content>"""


# ─────────────────────────── JSON 提取（同 s01） ───────────────────────────

def extract_json_text(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return text.strip()


# ─────────────────────────── Schema 校验（页面级） ───────────────────────────

LINES_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def validate_page_extraction(data, total_lines: int) -> list:
    """校验页面提取结果。total_lines 用于把行号引用和真实文件比对——
    引用了不存在的行 = 幻觉证据，必须打回。"""
    errors = []
    if not isinstance(data, dict):
        return ["顶层必须是 JSON 对象"]

    def check_str(field):
        if not isinstance(data.get(field), str) or not data[field].strip():
            errors.append(f'字段 "{field}" 缺失或不是非空字符串')

    def check_str_list(field, min_len):
        value = data.get(field)
        if not isinstance(value, list):
            errors.append(f'字段 "{field}" 缺失或不是数组')
        elif not all(isinstance(i, str) and i.strip() for i in value):
            errors.append(f'字段 "{field}" 的元素必须全部是非空字符串')
        elif len(value) < min_len:
            errors.append(f'字段 "{field}" 至少需要 {min_len} 个元素')

    check_str("title")
    check_str("summary")
    if data.get("page_type") not in PAGE_TYPE_DIRS:
        errors.append(f'字段 "page_type" 必须是以下之一：{", ".join(PAGE_TYPE_DIRS)}')
    check_str_list("responsibilities", 2)
    check_str_list("call_relations", 0)
    check_str_list("uncertainties", 0)

    ki = data.get("key_implementation")
    if not isinstance(ki, list) or len(ki) < 1:
        errors.append('字段 "key_implementation" 缺失或为空数组')
    else:
        for idx, item in enumerate(ki):
            if not isinstance(item, dict) or not isinstance(item.get("point"), str) \
                    or not item["point"].strip():
                errors.append(f'key_implementation[{idx}] 必须是含非空 "point" 的对象')
                continue
            m = LINES_RE.match(str(item.get("lines", "")))
            if not m:
                errors.append(f'key_implementation[{idx}].lines 格式必须是 "起-止" 或单行号，'
                              f'实际为 {item.get("lines")!r}')
                continue
            start, end = int(m.group(1)), int(m.group(2) or m.group(1))
            if start < 1 or end > total_lines or start > end:
                errors.append(f'key_implementation[{idx}].lines={item["lines"]} 超出文件'
                              f'真实行号范围 1-{total_lines}（引用必须可验证）')
    return errors


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

    def complete(self, messages: list) -> str:
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=0)
        return resp.choices[0].message.content or ""


class MockLLM:
    """第 1 次返回：page_type 不在枚举内 + 一条行号引用超出文件范围（幻觉证据）；
    第 2 次返回：修正后的合法结果。演示 s02 新增的两条校验规则。"""

    def __init__(self):
        self.calls = 0
        good = {
            "title": "Agent ReAct 引擎",
            "page_type": "module",
            "summary": ("engine.go 实现 WeKnora 的 ReAct Agent 引擎，是「模型即决策者」范式的核心。"
                        "它以 executeLoop 为主循环，每一轮调用 runReActIteration 执行 "
                        "think → act → observe，直到模型给出最终答案或达到 MaxIterations 上限。"
                        "循环走向由 iterOutcome 哨兵值控制。"),
            "responsibilities": [
                "驱动 ReAct 主循环并在每轮之间维护 Agent 状态",
                "在达到最大迭代数时兜底收尾，保证一定有输出",
                "把工具调用结果拼回对话历史供下一轮推理",
            ],
            "key_implementation": [
                {"point": "executeLoop 主循环，以 state.CurrentRound < MaxIterations 为边界",
                 "lines": "349-433"},
                {"point": "iterOutcome 哨兵值决定循环 continue/break，而非裸返回值",
                 "lines": "435-448"},
                {"point": "runReActIteration 单轮 think → analyze → act → observe",
                 "lines": "450-535"},
            ],
            "call_relations": [
                "调用 internal/agent/tools 的工具注册表执行工具",
                "使用 internal/types 的领域类型",
                "推测被 chat 服务层在 agent 模式下调用（需看 service 层证实）",
            ],
            "uncertainties": [
                "MaxIterations 的默认值来自何处（配置文件还是常量）未在本文件确认",
                "handleMaxIterations 的兜底输出策略细节需要展开阅读",
            ],
        }
        bad = dict(good)
        bad["page_type"] = "component"          # 不在枚举内
        bad["key_implementation"] = good["key_implementation"][:2] + [
            {"point": "finalize 阶段生成最终回答", "lines": "9000-9100"},  # 幻觉行号
        ]
        self.responses = [json.dumps(bad, ensure_ascii=False),
                          json.dumps(good, ensure_ascii=False)]

    def complete(self, messages: list) -> str:
        self.calls += 1
        idx = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[idx]


# ─────────────────────────── map：单文件 → 提取结果 ───────────────────────────

def number_lines(content: str) -> str:
    """给每行加行号前缀。行号让 LLM 的引用可验证——这是「重要事实必须有来源」
    从口号变成机制的关键一步。"""
    lines = content.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i + 1:>{width}}| {line}" for i, line in enumerate(lines))


def map_one_file(file_path: Path, llm) -> tuple:
    """教学版 mapOneDocument：守卫 → 读取 → 行号 → 截断 → 提取（带重试）。
    返回 (提取结果 dict, 文件总行数)。"""
    if is_sensitive_path(file_path):
        sys.exit(f"拒绝：{file_path.name} 匹配敏感文件排除清单，不会读取或发送给 LLM。")
    if not file_path.is_file():
        sys.exit(f"错误：文件不存在：{file_path}")
    try:
        file_path.resolve().relative_to(SOURCE_ROOT)
    except ValueError:
        sys.exit(f"错误：{file_path} 不在 SOURCE_ROOT（{SOURCE_ROOT}）内。"
                 "本项目只分析目标项目的文件。")

    content = file_path.read_text(encoding="utf-8", errors="replace")
    # 空内容守卫，对照 hasSufficientTextContent：没有实质文本就不调 LLM
    if len(re.sub(r"\s", "", content)) < MIN_TEXT_CHARS:
        sys.exit(f"跳过：{file_path.name} 实质内容不足 {MIN_TEXT_CHARS} 字符，"
                 "调用 LLM 只会得到幻觉。")

    total_lines = content.count("\n") + 1
    numbered = number_lines(content)
    if len(numbered) > MAX_CONTENT_CHARS:
        numbered = numbered[:MAX_CONTENT_CHARS]
        # 截断后 LLM 看不到尾部——可引用的行号范围也随之缩小
        total_lines = numbered.count("\n") + 1
        print(f"[截断] 带行号内容超过 {MAX_CONTENT_CHARS} 字符，截断至前 {total_lines} 行")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": EXTRACT_PAGE_PROMPT.format(
            path=file_path.relative_to(SOURCE_ROOT), content=numbered)},
    ]

    for attempt in range(1 + MAX_RETRIES):
        print(f"[调用] 第 {attempt + 1} 次 LLM 调用 ...")
        raw = llm.complete(messages)
        try:
            data = json.loads(extract_json_text(raw))
        except json.JSONDecodeError as e:
            errors = [f"JSON 解析失败：{e}"]
        else:
            errors = validate_page_extraction(data, total_lines)
            if not errors:
                print(f"[通过] schema 校验通过（第 {attempt + 1} 次尝试）")
                return data, total_lines
        print(f"[失败] 第 {attempt + 1} 次尝试未通过：{'; '.join(errors)}")
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content":
                         "你上一次的输出未通过校验，错误如下：\n- "
                         + "\n- ".join(errors)
                         + "\n\n请重新输出完整的 JSON 对象，修正以上全部错误。"
                         "只输出 JSON，不要围栏，不要解释。"})

    sys.exit(f"错误：重试 {MAX_RETRIES} 次后仍未通过 schema 校验，放弃。")


# ─────────────────────────── 渲染与落盘 ───────────────────────────

def slugify(rel_path: Path) -> str:
    """internal/agent/engine.go → internal-agent-engine。确定性：同一文件永远
    映射到同一页面路径，这是 s03 幂等重跑的前提。"""
    parts = list(rel_path.parts[:-1]) + [rel_path.stem]
    slug = "-".join(parts).lower()
    return re.sub(r"[^a-z0-9\-_]", "-", slug)


def render_page(rel_path: Path, data: dict) -> str:
    """提取结果 → 带 frontmatter 的 Markdown 页面。
    frontmatter 对照 types/wiki_page.go 的 PageType / Status / SourceRefs。"""
    today = datetime.date.today().isoformat()
    src = rel_path.as_posix()
    source_lines = [f'  - {{path: {src}, lines: {item["lines"]}}}'
                    for item in data["key_implementation"]]

    front = "\n".join([
        "---",
        f"title: {data['title']}",
        f"type: {data['page_type']}",
        "status: draft",                 # 初生成一律 draft；verified 要等人或 Agent 核实
        "sources:",
        *source_lines,
        f"updated_at: {today}",
        "---",
    ])

    def bullets(items):
        return "\n".join(f"- {i}" for i in items) if items else "（无）"

    key_impl = "\n".join(
        f'- {item["point"]}（来源：`{src}:{item["lines"]}`）'
        for item in data["key_implementation"])

    return f"""{front}

# {data['title']}

## 概述

{data['summary']}

## 核心职责

{bullets(data['responsibilities'])}

## 关键实现

{key_impl}

## 调用关系

{bullets(data['call_relations'])}

## 相关页面

（暂无——交叉链接由 s04 后处理注入）

## 证据与来源

本页面全部关键事实来自 `{src}`，各要点的行号引用见「关键实现」一节。

## 未确认事项

{bullets(data['uncertainties'])}
"""


def write_page(rel_path: Path, data: dict) -> Path:
    """写页面 + 追加修改日志。日志格式对照 workspace/logs/wiki-log.md 的约定：
    时间、页面、来源、原因、未确认事项。"""
    page_dir = WORKSPACE / "wiki" / PAGE_TYPE_DIRS[data["page_type"]]
    page_dir.mkdir(parents=True, exist_ok=True)
    page_path = page_dir / f"{slugify(rel_path)}.md"
    page_path.write_text(render_page(rel_path, data), encoding="utf-8", newline="\n")

    log_path = WORKSPACE / "logs" / "wiki-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (f"- {now} | {page_path.relative_to(WORKSPACE).as_posix()} | "
             f"来源 {rel_path.as_posix()} | 原因: map 生成 | "
             f"未确认事项: {len(data['uncertainties'])}\n")
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        if f.tell() == 0:
            f.write("# Wiki 修改日志\n\n")
        f.write(entry)
    return page_path


# ─────────────────────────── 入口 ───────────────────────────

def cmd_scan():
    if not SOURCE_ROOT.is_dir():
        sys.exit(f"错误：SOURCE_ROOT 不存在：{SOURCE_ROOT}")
    included, stats = scan_sources(SOURCE_ROOT)
    print(f"扫描 {SOURCE_ROOT}")
    print(f"  剪枝目录        : {stats['dir_pruned']}（.git/node_modules/frontend/...）")
    print(f"  敏感文件拒绝    : {stats['sensitive']}（.env/*.pem/...，连读都不读）")
    print(f"  扩展名过滤      : {stats['ext']}")
    print(f"  测试/生成码过滤 : {stats['pattern']}（*_test.go/*.pb.go/...）")
    print(f"  候选文件        : {len(included)}")
    print("\n前 20 个候选（s03 将把完整清单落为 manifest）：")
    for fpath in included[:20]:
        print(f"  {fpath.relative_to(SOURCE_ROOT).as_posix()}")


def cmd_map(file_arg: str, mock: bool):
    llm = MockLLM() if mock else OpenAILLM(load_env())
    file_path = Path(file_arg).resolve()
    data, _ = map_one_file(file_path, llm)
    page_path = write_page(file_path.relative_to(SOURCE_ROOT), data)
    print(f"\n[写入] {page_path}")
    print(f"[日志] {WORKSPACE / 'logs' / 'wiki-log.md'}")


def main():
    parser = argparse.ArgumentParser(description="s02：扫描源码，把单个文件 map 成 Wiki 页面")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="扫描 SOURCE_ROOT，打印候选文件与排除统计")
    p_map = sub.add_parser("map", help="对单个源文件提取并生成 Wiki 页面")
    p_map.add_argument("file", help="要分析的源文件路径（必须在 SOURCE_ROOT 内）")
    p_map.add_argument("--mock", action="store_true", help="使用 Mock LLM（无需 API Key）")
    args = parser.parse_args()

    if args.command == "scan":
        cmd_scan()
    else:
        cmd_map(args.file, args.mock)


if __name__ == "__main__":
    main()
