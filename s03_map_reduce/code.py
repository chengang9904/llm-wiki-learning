# -*- coding: utf-8 -*-
"""
s03：批量 map-reduce 与幂等状态

s02 一次处理一个文件、1:1 生成页面、重跑即重做。s03 把它升级为真正的批处理流水线：

  ingest：
    scan → manifest（哈希 + 已处理标记，重跑自动跳过）        [幂等]
         → 变更文件入队 pending.json（对照 task_pending_ops） [状态外置]
         → 循环认领批次（每批 5 个，对照 wikiMaxDocsPerBatch）[成本可控]
              map：逐文件提取主题更新（失败记 attempts，不中断整批）[失败隔离]
              reduce：按 slug 分组 → 读旧页面 + 新证据 → LLM 归并 → 写页面
         → 全部状态落盘，进程崩溃后重跑 ingest 即从断点继续    [崩溃恢复]

核心概念转变：s02 是「一个文件 → 一个页面」，s03 是「多个文件的证据 → 归并到
同一主题页面」。map 产出的是 SlugUpdate（对某主题页面的证据补充），
reduce 才是页面的唯一写入者——这正是 WeKnora mapOneDocument 返回 []SlugUpdate、
由 reduceSlugUpdates 统一落库的结构。

对照 WeKnora：
  - ProcessWikiIngest（wiki_ingest.go）——整个 ingest 命令的原型
  - claimPendingList（:667）/ task_pending_ops 表（带 dedup_key、fail_count）
  - wikiMaxDocsPerBatch = 5（:117）/ wikiMaxFailRetries = 5（:125）
  - mapOneDocument / reduceSlugUpdates / withSlugLock（教学版单进程不需要锁，见 README）
  - requeueFailedOps（:978）——失败重试与永久失败（dead-letter）

用法：
    python code.py ingest [--mock] [--path internal/agent] [--limit N]
                          [--mock-fail 子串] [--crash-after N]
    python code.py status
    python code.py retry          # 把永久失败的 op 重新入队（人工干预）
"""

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# ─────────────────────────── 路径与常量 ───────────────────────────

SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", r"C:\Desktop\Project\WeKnora"))
WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
STATE_DIR = WORKSPACE / "state"

MAX_CONTENT_CHARS = 32768
MAX_RETRIES = 2           # 单次 LLM 调用的 schema 重试上限（s01 起）
BATCH_SIZE = 5            # 对照 wikiMaxDocsPerBatch = 5
MAX_FAIL_RETRIES = 2      # 单个 op 的失败重试上限（对照 wikiMaxFailRetries = 5，教学版取 2）
MIN_TEXT_CHARS = 50

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
EXCLUDED_FILE_PATTERNS = ["*_test.go", "*.pb.go", "*_pb2.py", "*_pb2_grpc.py", "*.min.js"]
INCLUDE_EXTS = {".go", ".py", ".md", ".yaml", ".yml", ".proto"}

PAGE_TYPE_DIRS = {
    "architecture": "architecture", "module": "modules", "workflow": "workflows",
    "api": "api", "data": "data", "infrastructure": "infrastructure",
    "decision": "decisions", "glossary": "glossary",
}

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,63}$")
LINES_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_PATTERNS)


def scan_sources(root: Path):
    included = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if is_sensitive_path(fpath) or fpath.suffix.lower() not in INCLUDE_EXTS:
                continue
            if any(fnmatch.fnmatch(fname, pat) for pat in EXCLUDED_FILE_PATTERNS):
                continue
            included.append(fpath)
    return included


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


# ─────────────────────────── 状态外置：state/ 下的 JSON 文件 ───────────────────────────
# 所有进度都在磁盘上，进程内存里没有「只有我知道」的状态——这是崩溃恢复的全部秘密。
# 写入用「临时文件 + 原子替换」，崩在写入中途也不会留下半个 JSON。

def load_json(path: Path, default):
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class State:
    """sources.json（manifest）+ pending.json（op 队列）+ pages.json（页面元数据）。"""

    def __init__(self):
        self.sources = load_json(STATE_DIR / "sources.json", {"files": {}})
        self.pending = load_json(STATE_DIR / "pending.json", {"ops": [], "next_id": 1})
        self.pages = load_json(STATE_DIR / "pages.json", {"pages": {}})

    def save(self):
        save_json(STATE_DIR / "sources.json", self.sources)
        save_json(STATE_DIR / "pending.json", self.pending)
        save_json(STATE_DIR / "pages.json", self.pages)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ─────────────────────────── Prompt 模板 ───────────────────────────

SYSTEM_PROMPT = """你是一个代码分析器，为软件项目的 Wiki 生成页面素材。

### JSON Formatting Rules
- 只输出 JSON 对象本身，不要任何前言、解释或 Markdown 代码围栏。
- 字符串值内不要使用裸换行符；确需换行用 \\n 转义。
- 所有字段都必须出现，即使为空数组。

### 引用纪律
- 每条事实都必须带真实来源（行号 / 文件路径），不得发明来源。
- 没有把握的事实写进 uncertainties，不要写进正文字段。"""

# map：一个文件 → 若干「主题更新」。slug 是归并的键：模型优先复用已有 slug，
# 多个文件因此汇入同一页面。这对照 WeKnora 把 oldPageSlugs 传给提取器的做法。
MAP_PROMPT = """分析下面这个源文件（每行行首标了行号），把它的内容归入 1-3 个 Wiki 主题。
返回 JSON 对象：{{"topics": [主题更新, ...]}}，每个主题更新的字段：

- "slug": 字符串。主题页面的标识，小写字母/数字/连字符（如 "agent-engine"）。
  **优先复用下面「已有页面 slug」中的条目**；确属新主题才起新 slug。
- "title": 字符串。主题标题，中文名词短语。
- "page_type": 字符串。architecture | module | workflow | api | data |
  infrastructure | decision | glossary 之一。
- "summary": 字符串。本文件对这个主题贡献了什么信息，2-4 句话，中文。
- "responsibilities": 字符串数组。该主题的核心职责（从本文件可证实的），1-5 条。
- "facts": 对象数组，1-6 条：{{"point": "一句话事实", "lines": "起-止行号"}}。
  行号必须落在文件真实范围内。
- "relations": 字符串数组。调用/依赖关系，可为空。
- "uncertainties": 字符串数组。未确认事项，可为空。

已有页面 slug（优先复用）：{existing_slugs}

文件路径：{path}

文件内容（带行号，可能已截断）：
<file_content>
{content}
</file_content>"""

# reduce：旧页面 + 新证据 → 归并后的完整页面。这是「读-改-写」：
# 页面是累积的产物，不能只用新证据覆盖旧内容。
REDUCE_PROMPT = """你在维护 Wiki 页面「{slug}」。把新证据归并进旧页面，输出归并后的完整页面数据。

规则：
- 保留旧页面中仍然成立的事实，合并新证据，去掉重复；
- 每条 key_points 必须带 source_path 和 lines，只能来自旧页面或新证据中出现过的来源，
  不得发明；
- 新旧矛盾时以新证据为准，并把矛盾点记入 uncertainties。

返回 JSON 对象：
- "title": 字符串，中文标题
- "page_type": architecture | module | workflow | api | data | infrastructure |
  decision | glossary 之一
- "summary": 字符串。归并后的概述，3-6 句话
- "responsibilities": 字符串数组，2-8 条
- "key_points": 对象数组，2-40 条：{{"point": "...", "source_path": "...", "lines": "起-止"}}
- "call_relations": 字符串数组，可为空
- "uncertainties": 字符串数组，可为空

<old_page>
{old_page}
</old_page>

<evidence>
{evidence}
</evidence>"""


# ─────────────────────────── JSON 提取与校验 ───────────────────────────

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


def _check_str(data, field, errors):
    if not isinstance(data.get(field), str) or not data[field].strip():
        errors.append(f'字段 "{field}" 缺失或不是非空字符串')


def _check_str_list(data, field, min_len, errors):
    value = data.get(field)
    if not isinstance(value, list):
        errors.append(f'字段 "{field}" 缺失或不是数组')
    elif not all(isinstance(i, str) and i.strip() for i in value):
        errors.append(f'字段 "{field}" 的元素必须全部是非空字符串')
    elif len(value) < min_len:
        errors.append(f'字段 "{field}" 至少需要 {min_len} 个元素')


def validate_map(data, total_lines: int) -> list:
    errors = []
    if not isinstance(data, dict):
        return ["顶层必须是 JSON 对象"]
    topics = data.get("topics")
    if not isinstance(topics, list) or len(topics) < 1:
        return ['字段 "topics" 缺失或为空数组']
    for ti, topic in enumerate(topics):
        if not isinstance(topic, dict):
            errors.append(f"topics[{ti}] 必须是对象")
            continue
        if not SLUG_RE.match(str(topic.get("slug", ""))):
            errors.append(f'topics[{ti}].slug 必须匹配 [a-z0-9-]，实际 {topic.get("slug")!r}')
        _check_str(topic, "title", errors)
        _check_str(topic, "summary", errors)
        if topic.get("page_type") not in PAGE_TYPE_DIRS:
            errors.append(f'topics[{ti}].page_type 必须是：{", ".join(PAGE_TYPE_DIRS)}')
        _check_str_list(topic, "responsibilities", 1, errors)
        _check_str_list(topic, "relations", 0, errors)
        _check_str_list(topic, "uncertainties", 0, errors)
        facts = topic.get("facts")
        if not isinstance(facts, list) or len(facts) < 1:
            errors.append(f"topics[{ti}].facts 缺失或为空数组")
            continue
        for fi, fact in enumerate(facts):
            if not isinstance(fact, dict) or not isinstance(fact.get("point"), str) \
                    or not fact["point"].strip():
                errors.append(f"topics[{ti}].facts[{fi}] 必须是含非空 point 的对象")
                continue
            m = LINES_RE.match(str(fact.get("lines", "")))
            if not m:
                errors.append(f'topics[{ti}].facts[{fi}].lines 格式错误：{fact.get("lines")!r}')
                continue
            start, end = int(m.group(1)), int(m.group(2) or m.group(1))
            if start < 1 or end > total_lines or start > end:
                errors.append(f"topics[{ti}].facts[{fi}].lines={fact['lines']} "
                              f"超出文件真实行号范围 1-{total_lines}")
    return errors


def validate_reduce(data, allowed_paths: set) -> list:
    """归并结果校验。source_path 只能来自旧页面或本批证据——防止归并时发明来源。"""
    errors = []
    if not isinstance(data, dict):
        return ["顶层必须是 JSON 对象"]
    _check_str(data, "title", errors)
    _check_str(data, "summary", errors)
    if data.get("page_type") not in PAGE_TYPE_DIRS:
        errors.append(f'字段 "page_type" 必须是：{", ".join(PAGE_TYPE_DIRS)}')
    _check_str_list(data, "responsibilities", 1, errors)
    _check_str_list(data, "call_relations", 0, errors)
    _check_str_list(data, "uncertainties", 0, errors)
    kps = data.get("key_points")
    if not isinstance(kps, list) or len(kps) < 1:
        errors.append('字段 "key_points" 缺失或为空数组')
        return errors
    for i, kp in enumerate(kps):
        if not isinstance(kp, dict) or not isinstance(kp.get("point"), str) \
                or not kp["point"].strip():
            errors.append(f"key_points[{i}] 必须是含非空 point 的对象")
            continue
        if kp.get("source_path") not in allowed_paths:
            errors.append(f'key_points[{i}].source_path={kp.get("source_path")!r} '
                          f"不在允许的来源集合内（不得发明来源）")
        if not LINES_RE.match(str(kp.get("lines", ""))):
            errors.append(f'key_points[{i}].lines 格式错误：{kp.get("lines")!r}')
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
    """确定性 Mock，让整条 map-reduce 流水线无需 API Key 即可端到端跑通。

    - map：从 prompt 里解析文件路径与带行号内容，用正则找出 func/def/type 定义
      （行号是真实的，天然通过范围校验）；slug 从文件所在目录导出——
      同目录的文件因此归入同一主题页面，正好演示 reduce 归并；
    - reduce：解析 <evidence> 里的证据 JSON，机械合并出页面数据；
    - mock_fail：路径含指定子串的文件永远返回非 JSON → 演示失败隔离与重试耗尽。

    真实 LLM 会给出语义化的 slug 和有洞察的归并；Mock 只保证结构正确、来源真实。
    """

    DEF_RE = re.compile(r"^\s*(\d+)\|\s*(?:func|def|type|class)\s+(?:\([^)]*\)\s*)?(\w+)",
                        re.MULTILINE)

    def __init__(self, mock_fail: str = ""):
        self.mock_fail = mock_fail

    def complete(self, messages: list) -> str:
        # 重试时对话末尾是「错误反馈」消息，任务本体在更早的 user 消息里——
        # 从对话中找到带任务标记的那条（真实 LLM 天然看完整对话，mock 也必须如此）
        prompt = next((m["content"] for m in messages
                       if m["role"] == "user"
                       and ("<file_content>" in m["content"] or "<evidence>" in m["content"])),
                      messages[-1]["content"])
        if "<evidence>" in prompt:
            return self._reduce(prompt)
        return self._map(prompt)

    def _map(self, prompt: str) -> str:
        path = re.search(r"文件路径：(.+)", prompt).group(1).strip()
        if self.mock_fail and self.mock_fail in path:
            return "抱歉，我无法完成这个任务。（mock 注入的持续失败）"
        content = prompt.split("<file_content>")[1].split("</file_content>")[0]
        rel = Path(path)
        # slug 从目录导出：internal/agent/engine.go → internal-agent。
        # 这让同目录文件汇入同一页面，是 mock 版的「主题归并」。
        slug = "-".join(rel.parts[:-1]).lower() or rel.stem.lower()
        slug = re.sub(r"[^a-z0-9\-]", "-", slug)[:64].strip("-") or "misc"
        defs = self.DEF_RE.findall(content)[:5]
        facts = [{"point": f"定义 {name}（第 {line} 行起）", "lines": line}
                 for line, name in defs]
        if not facts:
            first_line = content.strip().splitlines()[0] if content.strip() else "1| （空）"
            lineno = first_line.split("|")[0].strip() or "1"
            facts = [{"point": f"文件 {rel.name} 的内容（mock 摘要）", "lines": lineno}]
        return json.dumps({"topics": [{
            "slug": slug,
            "title": f"模块 {slug}",
            "page_type": "module",
            "summary": f"{path} 属于主题 {slug}，本文件贡献 {len(facts)} 条证据（mock 生成）。",
            "responsibilities": [f"提供 {rel.name} 中实现的能力（mock）"],
            "facts": facts,
            "relations": [],
            "uncertainties": [f"{rel.name} 的语义摘要需要真实 LLM 生成（mock 占位）"],
        }]}, ensure_ascii=False)

    # 旧页面「关键实现」条目的格式：- 事实（来源：`path:lines`）
    OLD_POINT_RE = re.compile(r"^- (.+?)（来源：`([^:`]+):([\d\-]+)`）", re.MULTILINE)

    def _reduce(self, prompt: str) -> str:
        evidence = json.loads(prompt.split("<evidence>")[1].split("</evidence>")[0])
        old_page = prompt.split("<old_page>")[1].split("</old_page>")[0]
        title = evidence[0]["title"]
        key_points, resp, unc, seen = [], [], [], set()
        # 读-改-写的「读」：先把旧页面已有的事实抬进来，再叠加新证据。
        # 不做这一步就是 lost update——第二批归并会把第一批的成果覆盖掉。
        for point, path, lines in self.OLD_POINT_RE.findall(old_page):
            key = (path, lines, point)
            if key not in seen:
                seen.add(key)
                key_points.append({"point": point, "source_path": path, "lines": lines})
        for update in evidence:
            for fact in update["facts"]:
                key = (update["source_path"], fact["lines"], fact["point"])
                if key not in seen:
                    seen.add(key)
                    key_points.append({"point": fact["point"],
                                       "source_path": update["source_path"],
                                       "lines": fact["lines"]})
            resp.extend(r for r in update["responsibilities"] if r not in resp)
            unc.extend(u for u in update["uncertainties"] if u not in unc)
        return json.dumps({
            "title": title,
            "page_type": evidence[0]["page_type"],
            "summary": "；".join(dict.fromkeys(u["summary"] for u in evidence)),
            "responsibilities": resp or ["（mock 未能提取职责）"],
            "key_points": key_points[:40],
            "call_relations": [],
            "uncertainties": unc,
        }, ensure_ascii=False)


# ─────────────────────────── 带重试的结构化调用（s01 的骨架） ───────────────────────────

def call_structured(llm, messages: list, validate, label: str):
    """调用 → 解析 → 校验 → 带错误重试。返回 dict；耗尽重试返回 None（不再退出进程——
    s03 的失败要被隔离，不能让一个文件毁掉整批）。"""
    for attempt in range(1 + MAX_RETRIES):
        raw = llm.complete(messages)
        try:
            data = json.loads(extract_json_text(raw))
        except json.JSONDecodeError as e:
            errors = [f"JSON 解析失败：{e}"]
        else:
            errors = validate(data)
            if not errors:
                return data
        print(f"    [失败] {label} 第 {attempt + 1} 次尝试：{'; '.join(errors[:3])}")
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content":
                         "你上一次的输出未通过校验，错误如下：\n- " + "\n- ".join(errors)
                         + "\n\n请重新输出完整的 JSON 对象，修正以上全部错误。"
                         "只输出 JSON，不要围栏，不要解释。"})
    return None


# ─────────────────────────── map：一个 op → 主题更新列表 ───────────────────────────

def number_lines(content: str) -> str:
    lines = content.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i + 1:>{width}}| {line}" for i, line in enumerate(lines))


def map_one(op: dict, llm, existing_slugs: list):
    """教学版 mapOneDocument。返回 SlugUpdate 列表（含 source_path），失败返回 None。"""
    fpath = SOURCE_ROOT / op["path"]
    if not fpath.is_file():
        print(f"    [跳过] {op['path']} 已不存在")
        return []
    content = fpath.read_text(encoding="utf-8", errors="replace")
    if len(re.sub(r"\s", "", content)) < MIN_TEXT_CHARS:
        print(f"    [跳过] {op['path']} 实质内容不足")
        return []
    total_lines = content.count("\n") + 1
    numbered = number_lines(content)
    if len(numbered) > MAX_CONTENT_CHARS:
        numbered = numbered[:MAX_CONTENT_CHARS]
        total_lines = numbered.count("\n") + 1

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": MAP_PROMPT.format(
            existing_slugs=", ".join(existing_slugs) or "（暂无）",
            path=op["path"], content=numbered)},
    ]
    data = call_structured(llm, messages,
                           lambda d: validate_map(d, total_lines), f"map {op['path']}")
    if data is None:
        return None
    for topic in data["topics"]:
        topic["source_path"] = op["path"]     # 证据必须记住自己来自哪个文件
    return data["topics"]


# ─────────────────────────── reduce：按 slug 归并到页面 ───────────────────────────

def page_path_for(slug: str, page_type: str) -> Path:
    return WORKSPACE / "wiki" / PAGE_TYPE_DIRS[page_type] / f"{slug}.md"


def reduce_slug(slug: str, updates: list, llm, state: State):
    """教学版 reduceSlugUpdates：读旧页面 + 新证据 → LLM 归并 → 写页面。
    WeKnora 在这里包了 withSlugLock（并发批次会同时更新同一 slug）；
    教学版单进程顺序执行，同一时刻只有这一处在「读-改-写」，天然无竞争。"""
    meta = state.pages["pages"].get(slug)
    old_page = "（无旧页面，这是该主题的第一批证据）"
    old_paths = set()
    if meta:
        old_file = WORKSPACE / meta["path"]
        if old_file.is_file():
            old_page = old_file.read_text(encoding="utf-8")
        old_paths = set(meta.get("sources", []))

    allowed_paths = old_paths | {u["source_path"] for u in updates}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": REDUCE_PROMPT.format(
            slug=slug, old_page=old_page,
            evidence=json.dumps(updates, ensure_ascii=False, indent=1))},
    ]
    data = call_structured(llm, messages,
                           lambda d: validate_reduce(d, allowed_paths), f"reduce {slug}")
    if data is None:
        return False

    # 渲染并写入页面（reduce 是页面的唯一写入者）
    today = datetime.date.today().isoformat()
    sources_meta = sorted(allowed_paths)
    front = "\n".join([
        "---",
        f"title: {data['title']}",
        f"type: {data['page_type']}",
        "status: draft",
        "sources:",
        *[f'  - {{path: {kp["source_path"]}, lines: {kp["lines"]}}}'
          for kp in data["key_points"]],
        f"updated_at: {today}",
        "---",
    ])

    def bullets(items):
        return "\n".join(f"- {i}" for i in items) if items else "（无）"

    key_points = "\n".join(
        f'- {kp["point"]}（来源：`{kp["source_path"]}:{kp["lines"]}`）'
        for kp in data["key_points"])
    body = f"""{front}

# {data['title']}

## 概述

{data['summary']}

## 核心职责

{bullets(data['responsibilities'])}

## 关键实现

{key_points}

## 调用关系

{bullets(data['call_relations'])}

## 相关页面

（暂无——交叉链接由 s04 后处理注入）

## 证据与来源

本页面证据来自 {len(sources_meta)} 个源文件：{"、".join(f"`{p}`" for p in sources_meta)}。

## 未确认事项

{bullets(data['uncertainties'])}
"""
    out = page_path_for(slug, data["page_type"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8", newline="\n")

    state.pages["pages"][slug] = {
        "title": data["title"], "page_type": data["page_type"],
        "path": out.relative_to(WORKSPACE).as_posix(),
        "sources": sources_meta, "updated_at": today,
    }
    append_log(out.relative_to(WORKSPACE).as_posix(),
               f"reduce 归并（本批 {len(updates)} 条证据，共 {len(sources_meta)} 个来源）",
               len(data["uncertainties"]))
    return True


def append_log(page_rel: str, reason: str, uncertainties: int):
    log_path = WORKSPACE / "logs" / "wiki-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        if f.tell() == 0:
            f.write("# Wiki 修改日志\n\n")
        f.write(f"- {now} | {page_rel} | 原因: {reason} | 未确认事项: {uncertainties}\n")


# ─────────────────────────── ingest：完整流水线 ───────────────────────────

def cmd_ingest(args):
    llm = MockLLM(args.mock_fail) if args.mock else OpenAILLM(load_env())
    state = State()

    # 1) scan + manifest：哈希对比找出「新增或变更」的文件（幂等的判定基础）
    print(f"[scan] 扫描 {SOURCE_ROOT} ...")
    included = scan_sources(SOURCE_ROOT)
    if args.path:
        prefix = args.path.replace("\\", "/").rstrip("/") + "/"
        included = [f for f in included
                    if f.relative_to(SOURCE_ROOT).as_posix().startswith(prefix)]
    changed = []
    for fpath in included:
        rel = fpath.relative_to(SOURCE_ROOT).as_posix()
        digest = file_hash(fpath)
        entry = state.sources["files"].setdefault(rel, {})
        entry["hash"], entry["mtime"] = digest, fpath.stat().st_mtime
        if entry.get("processed_hash") != digest:
            changed.append((rel, digest))
    print(f"[scan] 候选 {len(included)}，其中新增/变更 {len(changed)}，"
          f"已处理跳过 {len(included) - len(changed)}")

    if args.limit:
        changed = changed[:args.limit]
        print(f"[scan] --limit {args.limit}：本次只入队前 {len(changed)} 个")

    # 2) 入队（带去重，对照 task_pending_ops 的 dedup_key：同一 path+hash 不重复入队）
    existing_keys = {(op["path"], op["hash"]) for op in state.pending["ops"]
                     if op["status"] in ("pending", "failed")}
    enqueued = 0
    for rel, digest in changed:
        if (rel, digest) in existing_keys:
            continue
        state.pending["ops"].append({
            "id": state.pending["next_id"], "path": rel, "hash": digest,
            "status": "pending", "attempts": 0, "error": None,
        })
        state.pending["next_id"] += 1
        enqueued += 1
    state.save()
    print(f"[queue] 新入队 {enqueued}，队列中 pending "
          f"{sum(1 for o in state.pending['ops'] if o['status'] == 'pending')}")

    # 3) 批处理循环：认领 → map → reduce → 标记 done。每批结束即存盘，
    #    崩溃后重跑 ingest 就从未完成的 op 继续（对照 ProcessWikiIngest 的批循环）。
    mapped_count = 0
    batch_no = 0
    while True:
        batch = [op for op in state.pending["ops"]
                 if op["status"] == "pending"][:BATCH_SIZE]     # 对照 claimPendingList
        if not batch:
            break
        batch_no += 1
        print(f"\n[batch {batch_no}] 认领 {len(batch)} 个 op（批大小上限 {BATCH_SIZE}）")

        slug_updates = {}          # slug → [update...]，本批的 map 产物
        op_slugs = {}              # op id → 贡献到的 slug 集合
        existing_slugs = sorted(state.pages["pages"])
        for op in batch:
            print(f"  [map] #{op['id']} {op['path']}")
            topics = map_one(op, llm, existing_slugs)
            mapped_count += 1
            if args.crash_after and mapped_count >= args.crash_after:
                print(f"\n[crash] --crash-after {args.crash_after}：模拟进程崩溃！"
                      "（op 仍是 pending，重跑 ingest 即从断点继续）")
                sys.stdout.flush()      # os._exit 不走正常退出流程，手动刷缓冲
                os._exit(99)
            if topics is None:                       # 失败隔离：记账，不中断整批
                op["attempts"] += 1
                if op["attempts"] >= MAX_FAIL_RETRIES:   # 对照 requeueFailedOps 的
                    op["status"] = "failed"              # 超限永久失败（dead-letter）
                    op["error"] = f"map 重试 {op['attempts']} 次仍失败"
                    print(f"    [dead] #{op['id']} 永久失败（attempts={op['attempts']}），"
                          "可用 retry 命令人工重新入队")
                else:
                    print(f"    [requeue] #{op['id']} 留在队列等待重试"
                          f"（attempts={op['attempts']}/{MAX_FAIL_RETRIES}）")
                state.save()
                continue
            op_slugs[op["id"]] = {t["slug"] for t in topics}
            for topic in topics:
                slug_updates.setdefault(topic["slug"], []).append(topic)

        # reduce：按 slug 归并（教学版顺序执行 = 免锁；WeKnora 并发批次需要 withSlugLock）
        failed_slugs = set()
        for slug, updates in sorted(slug_updates.items()):
            print(f"  [reduce] {slug} ← {len(updates)} 条更新")
            if not reduce_slug(slug, updates, llm, state):
                failed_slugs.add(slug)

        # 标记完成：op 的全部 slug 都归并成功才算 done，并回写 manifest 已处理标记
        for op in batch:
            slugs = op_slugs.get(op["id"])
            if slugs is None:
                continue                              # map 已失败，前面处理过
            if slugs & failed_slugs:
                op["attempts"] += 1
                op["status"] = "failed" if op["attempts"] >= MAX_FAIL_RETRIES else "pending"
                op["error"] = f"reduce 失败：{', '.join(slugs & failed_slugs)}"
            else:
                op["status"] = "done"
                state.sources["files"][op["path"]]["processed_hash"] = op["hash"]
        state.save()                                  # 每批落盘 = 断点

    done = sum(1 for o in state.pending["ops"] if o["status"] == "done")
    failed = sum(1 for o in state.pending["ops"] if o["status"] == "failed")
    print(f"\n[完成] done={done} failed={failed} 页面={len(state.pages['pages'])}"
          + ("（失败 op 用 `python code.py retry` 重新入队）" if failed else ""))


# ─────────────────────────── status / retry ───────────────────────────

def cmd_status():
    state = State()
    files = state.sources["files"]
    processed = sum(1 for f in files.values() if f.get("processed_hash") == f.get("hash"))
    print(f"manifest : {len(files)} 个文件，已处理 {processed}")
    by_status = {}
    for op in state.pending["ops"]:
        by_status[op["status"]] = by_status.get(op["status"], 0) + 1
    print(f"op 队列  : {by_status or '（空）'}")
    for op in state.pending["ops"]:
        if op["status"] == "failed":
            print(f"  failed #{op['id']} {op['path']} attempts={op['attempts']} "
                  f"error={op['error']}")
    print(f"页面     : {len(state.pages['pages'])} 个")
    for slug, meta in sorted(state.pages["pages"].items()):
        print(f"  {slug} ← {len(meta['sources'])} 个来源（{meta['path']}）")


def cmd_retry():
    """人工把永久失败的 op 重新入队（对照 dead-letter 的人工重放）。"""
    state = State()
    count = 0
    for op in state.pending["ops"]:
        if op["status"] == "failed":
            op["status"], op["attempts"], op["error"] = "pending", 0, None
            count += 1
            print(f"[requeue] #{op['id']} {op['path']}")
    state.save()
    print(f"[完成] 重新入队 {count} 个 op，重新运行 ingest 即可处理")


def main():
    parser = argparse.ArgumentParser(description="s03：批量 map-reduce 与幂等状态")
    sub = parser.add_subparsers(dest="command", required=True)
    p_ingest = sub.add_parser("ingest", help="扫描并批量生成/更新 Wiki 页面（可断点续跑）")
    p_ingest.add_argument("--mock", action="store_true", help="使用 Mock LLM")
    p_ingest.add_argument("--path", help="只处理 SOURCE_ROOT 下此相对路径前缀（如 internal/agent）")
    p_ingest.add_argument("--limit", type=int, default=10,
                          help="本次最多入队的文件数（成本护栏，默认 10，0 = 不限）")
    p_ingest.add_argument("--mock-fail", default="",
                          help="（演示用）路径含此子串的文件 mock 永远失败")
    p_ingest.add_argument("--crash-after", type=int, default=0,
                          help="（演示用）map 完 N 个 op 后模拟进程崩溃")
    sub.add_parser("status", help="查看 manifest / 队列 / 页面状态")
    sub.add_parser("retry", help="把永久失败的 op 重新入队")
    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "status":
        cmd_status()
    else:
        cmd_retry()


if __name__ == "__main__":
    main()
