# -*- coding: utf-8 -*-
"""
s04：后处理流水线

s03 产出的页面是「各自为政」的：可能两个页面讲同一主题（s02 的 internal-agent-engine
和 s03 的 internal-agent）、页面之间没有链接、没有全局入口。s04 用一条**确定性步骤链**
把散页收拾成一个 Wiki：

  python code.py postprocess
    ├─ 0 discover   扫描 wiki/ 全部页面，解析 frontmatter，同步 pages.json
    ├─ 1 dedup      去重合并：候选对由代码筛（来源重叠/slug 相似），
    │               定夺由一次 LLM 调用；合并后留别名与重定向记录
    ├─ 2 crosslink  交叉链接注入：标题/别名做纯文本匹配 → 内联链接；
    │               重建每页「相关页面」小节（共享来源/共享目录/被链接）
    ├─ 3 deadlink   清理死链：指向已合并页面的链接改写到重定向目标，
    │               指向不存在页面的链接解除为纯文本
    └─ 4 index      重建 index.md：分组目录（代码），导读（一次 LLM 调用，
                    页面集合未变化时用缓存，不重复付费）

注意本阶段的 LLM 用量：只有 dedup 定夺和 index 导读两处，其余全是确定性代码。
「候选由代码筛、定夺由 LLM」是流水线控制成本的标准姿势——
代码把 O(n²) 的比较缩到几对，模型只对筛出的候选做一次判断。

对照 WeKnora：
  - deduplicateExtractedBatch（wiki_ingest.go:2086）
  - linkifyContent（wiki_linkify.go）——注入 [[slug|文本]]，跳过已有链接/代码段；
    教学版用标准 Markdown 相对链接替代 [[...]] 语法
  - cleanDeadLinks（wiki_ingest.go:1484）/ sanitizeDeadSummaryLinks
  - rebuildIndexPage（wiki_ingest.go:1876）
  - planBatchTaxonomy（wiki_ingest_taxonomy.go:32）——教学版退化为 type→目录 的固定映射
  - 页面 Aliases（tools/wiki_write_page.go）

用法：
    python code.py postprocess [--mock]
"""

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

# ─────────────────────────── 路径与常量 ───────────────────────────

WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
WIKI_DIR = WORKSPACE / "wiki"
STATE_DIR = WORKSPACE / "state"

MAX_RETRIES = 2
MAX_DEDUP_PAIRS = 5        # 每次运行最多送多少对候选给 LLM 定夺（成本护栏）
MAX_PAGE_CHARS = 6000      # dedup prompt 里每个页面的截断上限
SIMILARITY_THRESHOLD = 0.7 # slug/标题相似度达到该值即成为候选对
MIN_TERM_LEN = 4           # 短于 4 个字符的标题/别名不做内联匹配（噪音太多）

PAGE_TYPE_DIRS = {
    "architecture": "architecture", "module": "modules", "workflow": "workflows",
    "api": "api", "data": "data", "infrastructure": "infrastructure",
    "decision": "decisions", "glossary": "glossary",
}
LINES_RE = re.compile(r"^(\d+)(?:-(\d+))?$")
SOURCE_LINE_RE = re.compile(r"-\s*\{path:\s*([^,}]+),\s*lines:\s*([^}]+)\}")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)#\s]+\.md)\)")


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


class State:
    """pages.json 在 s04 里扩展：每页多了 aliases / links，顶层多了
    redirects（合并留下的转发记录）、dedup_checked（已定夺过的候选对，幂等用）、
    index_cache（导读缓存）。"""

    def __init__(self):
        self.pages = load_json(STATE_DIR / "pages.json", {"pages": {}})
        self.pages.setdefault("redirects", {})
        self.pages.setdefault("dedup_checked", [])
        self.pages.setdefault("index_cache", {})

    def save(self):
        save_json(STATE_DIR / "pages.json", self.pages)


def append_log(page_rel: str, reason: str, uncertainties: int = 0):
    log_path = WORKSPACE / "logs" / "wiki-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        if f.tell() == 0:
            f.write("# Wiki 修改日志\n\n")
        f.write(f"- {now} | {page_rel} | 原因: {reason} | 未确认事项: {uncertainties}\n")


# ─────────────────────────── 页面解析 ───────────────────────────

def parse_page(path: Path) -> dict:
    """解析一个 Wiki 页面：frontmatter 字段 + 正文。返回 None 表示无 frontmatter。"""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    front_text, body = parts[1], parts[2].lstrip("\n")
    front = {}
    for line in front_text.splitlines():
        if ":" in line and not line.strip().startswith("-"):
            key, _, value = line.partition(":")
            front[key.strip()] = value.strip()
    sources = [(m.group(1).strip(), m.group(2).strip())
               for m in SOURCE_LINE_RE.finditer(front_text)]
    return {
        "path": path,
        "slug": path.stem,
        "title": front.get("title", path.stem),
        "page_type": front.get("type", "module"),
        "status": front.get("status", "draft"),
        "sources": sources,
        "front_text": front_text,
        "body": body,
    }


def all_wiki_pages():
    pages = []
    for sub in PAGE_TYPE_DIRS.values():
        d = WIKI_DIR / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            parsed = parse_page(f)
            if parsed:
                pages.append(parsed)
    return pages


def write_page_text(page: dict):
    """把（可能被修改过的）frontmatter + 正文写回磁盘。"""
    page["path"].write_text("---" + page["front_text"] + "---\n\n" + page["body"],
                            encoding="utf-8", newline="\n")


def section_replace(body: str, heading: str, new_content: str) -> str:
    """替换 body 中某个 `## 标题` 小节的内容（保留标题行，直到下一个 ## 或结尾）。
    小节不存在则追加到末尾。每次整节重建 → 天然幂等。"""
    pattern = re.compile(rf"(^## {re.escape(heading)}\n)(.*?)(?=^## |\Z)",
                         re.MULTILINE | re.DOTALL)
    replacement = rf"\g<1>\n{new_content}\n\n"
    if pattern.search(body):
        return pattern.sub(replacement, body, count=1)
    return body.rstrip() + f"\n\n## {heading}\n\n{new_content}\n"


# ─────────────────────────── LLM ───────────────────────────

SYSTEM_PROMPT = """你是一个 Wiki 维护助手。

### JSON Formatting Rules
- 只输出 JSON 对象本身，不要任何前言、解释或 Markdown 代码围栏。
- 字符串值内不要使用裸换行符；确需换行用 \\n 转义。
- 所有字段都必须出现（不适用时给 null 或空数组）。"""

DEDUP_PROMPT = """下面是同一个 Wiki 里的两个页面，代码筛查发现它们可能讲的是同一主题
（来源重叠或标题相似）。请判断是否应当合并。

规则：
- 只有当两个页面**核心主题相同**时才合并；主题相关但不相同（如「模块」与其「子模块」）不合并；
- 合并时保留信息量更大的 slug 作为 primary_slug；
- 合并后的页面必须保留双方所有仍然成立的事实与来源引用，不得发明来源；
- 双方矛盾的事实记入 uncertainties。

返回 JSON：
- "merge": true 或 false
- "reason": 字符串，判断理由（一句话）
- "primary_slug": 合并时保留的 slug（必须是 {slug_a} 或 {slug_b}）；不合并时为 null
- "merged": 不合并时为 null；合并时为完整页面对象：
  {{"title": "...", "page_type": "...", "summary": "...",
    "responsibilities": [...],
    "key_points": [{{"point": "...", "source_path": "...", "lines": "起-止"}}, ...],
    "call_relations": [...], "uncertainties": [...]}}

<page_a slug="{slug_a}">
{page_a}
</page_a>

<page_b slug="{slug_b}">
{page_b}
</page_b>"""

INDEX_INTRO_PROMPT = """下面是一个软件项目 Wiki 的页面目录。写一段 3-5 句话的中文导读，
说明这个 Wiki 覆盖了哪些方面、读者应该从哪里读起。返回 JSON：{{"intro": "..."}}

<index_toc>
{toc}
</index_toc>"""


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


def validate_judge(data, slug_a: str, slug_b: str, allowed_paths: set) -> list:
    errors = []
    if not isinstance(data, dict):
        return ["顶层必须是 JSON 对象"]
    if not isinstance(data.get("merge"), bool):
        errors.append('字段 "merge" 必须是 true/false')
        return errors
    _check_str(data, "reason", errors)
    if not data["merge"]:
        return errors
    if data.get("primary_slug") not in (slug_a, slug_b):
        errors.append(f'primary_slug 必须是 {slug_a} 或 {slug_b}')
    merged = data.get("merged")
    if not isinstance(merged, dict):
        errors.append('合并时 "merged" 必须是页面对象')
        return errors
    _check_str(merged, "title", errors)
    _check_str(merged, "summary", errors)
    if merged.get("page_type") not in PAGE_TYPE_DIRS:
        errors.append(f'merged.page_type 必须是：{", ".join(PAGE_TYPE_DIRS)}')
    _check_str_list(merged, "responsibilities", 1, errors)
    _check_str_list(merged, "call_relations", 0, errors)
    _check_str_list(merged, "uncertainties", 0, errors)
    kps = merged.get("key_points")
    if not isinstance(kps, list) or len(kps) < 1:
        errors.append("merged.key_points 缺失或为空数组")
        return errors
    for i, kp in enumerate(kps):
        if not isinstance(kp, dict) or not str(kp.get("point", "")).strip():
            errors.append(f"merged.key_points[{i}] 必须是含非空 point 的对象")
            continue
        if kp.get("source_path") not in allowed_paths:
            errors.append(f'merged.key_points[{i}].source_path={kp.get("source_path")!r} '
                          "不在两个页面的来源集合内（不得发明来源）")
        if not LINES_RE.match(str(kp.get("lines", ""))):
            errors.append(f'merged.key_points[{i}].lines 格式错误')
    return errors


def validate_intro(data) -> list:
    errors = []
    if not isinstance(data, dict):
        return ["顶层必须是 JSON 对象"]
    _check_str(data, "intro", errors)
    return errors


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
    """dedup 定夺：来源有重叠 → 合并（primary = 来源更多的一方，机械合并双方要点）；
    无重叠 → 不合并。index 导读：固定文本。"""

    POINT_RE = re.compile(r"^- (.+?)（来源：`([^:`]+):([\d\-]+)`）", re.MULTILINE)

    def complete(self, messages: list) -> str:
        prompt = next((m["content"] for m in messages
                       if m["role"] == "user"
                       and ("<page_a" in m["content"] or "<index_toc>" in m["content"])),
                      messages[-1]["content"])
        if "<index_toc>" in prompt:
            return json.dumps({"intro": "本 Wiki 由确定性流水线从 WeKnora 源码生成，"
                               "覆盖后端模块与启动流程。建议从 architecture 分组读起，"
                               "再按模块页面下钻；每条关键事实都带行号来源。（mock 导读）"},
                              ensure_ascii=False)
        return self._judge(prompt)

    def _judge(self, prompt: str) -> str:
        def block(tag):
            m = re.search(rf'<{tag} slug="([^"]+)">\n(.*?)</{tag}>', prompt, re.DOTALL)
            return m.group(1), m.group(2)

        slug_a, page_a = block("page_a")
        slug_b, page_b = block("page_b")
        src_a = {m.group(1).strip() for m in SOURCE_LINE_RE.finditer(page_a)}
        src_b = {m.group(1).strip() for m in SOURCE_LINE_RE.finditer(page_b)}
        overlap = src_a & src_b
        if not overlap:
            return json.dumps({"merge": False,
                               "reason": "来源无重叠，判定为相关但不相同的主题（mock）",
                               "primary_slug": None, "merged": None}, ensure_ascii=False)

        primary = slug_a if len(src_a) >= len(src_b) else slug_b
        pri_page = page_a if primary == slug_a else page_b

        def summary_of(page):
            m = re.search(r"## 概述\n+(.+?)(?=\n## |\Z)", page, re.DOTALL)
            return m.group(1).strip().replace("\n", " ") if m else ""

        def bullets_of(page, heading):
            m = re.search(rf"## {heading}\n(.*?)(?=^## |\Z)", page, re.MULTILINE | re.DOTALL)
            return [ln[2:].strip() for ln in (m.group(1) if m else "").splitlines()
                    if ln.startswith("- ")]

        key_points, seen = [], set()
        for page in (pri_page, page_b if primary == slug_a else page_a):
            for point, path, lines in self.POINT_RE.findall(page):
                if (path, lines) not in seen:
                    seen.add((path, lines))
                    key_points.append({"point": point, "source_path": path, "lines": lines})
        resp = []
        for page in (page_a, page_b):
            for b in bullets_of(page, "核心职责"):
                if b not in resp:
                    resp.append(b)
        title_m = re.search(r"^# (.+)$", pri_page, re.MULTILINE)
        return json.dumps({
            "merge": True,
            "reason": f"来源重叠（{', '.join(sorted(overlap))}），判定为同一主题（mock）",
            "primary_slug": primary,
            "merged": {
                "title": title_m.group(1) if title_m else primary,
                "page_type": "module",
                "summary": (summary_of(page_a) + " " + summary_of(page_b)).strip()[:800],
                "responsibilities": resp or ["（mock 合并）"],
                "key_points": key_points,
                "call_relations": [],
                "uncertainties": ["合并页面的语义归纳需要真实 LLM（mock 机械合并）"],
            },
        }, ensure_ascii=False)


def call_structured(llm, messages: list, validate, label: str):
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
                         + "\n\n请重新输出完整的 JSON 对象，修正以上全部错误。"})
    return None


# ─────────────────────────── 步骤 0：discover ───────────────────────────

def step_discover(state: State):
    """扫描磁盘上的全部页面，同步 pages.json——磁盘是事实，状态是缓存。
    这样 s02 手工生成的页面、Agent 后续写的页面都会被后处理接管。"""
    pages = all_wiki_pages()
    known = state.pages["pages"]
    for p in pages:
        entry = known.setdefault(p["slug"], {})
        entry.update({
            "title": p["title"], "page_type": p["page_type"],
            "path": p["path"].relative_to(WORKSPACE).as_posix(),
            "sources": sorted({src for src, _ in p["sources"]}),
        })
        entry.setdefault("aliases", [])
        entry.setdefault("updated_at", datetime.date.today().isoformat())
    # 磁盘上已不存在的条目移除（可能被合并/删除）
    disk_slugs = {p["slug"] for p in pages}
    for slug in [s for s in known if s not in disk_slugs]:
        del known[slug]
    print(f"[discover] 磁盘页面 {len(pages)} 个，pages.json 已同步")
    return pages


# ─────────────────────────── 步骤 1：dedup ───────────────────────────

def find_dedup_candidates(pages: list, state: State):
    """候选对由代码筛：来源重叠，或 slug/标题相似度超阈值。O(n²) 但 n 是页面数，
    且真正花钱的 LLM 只看筛出的前几对。"""
    checked = set(state.pages["dedup_checked"])
    candidates = []
    for i in range(len(pages)):
        for j in range(i + 1, len(pages)):
            a, b = pages[i], pages[j]
            key = "|".join(sorted([a["slug"], b["slug"]]))
            if key in checked:
                continue
            src_a = {s for s, _ in a["sources"]}
            src_b = {s for s, _ in b["sources"]}
            overlap = len(src_a & src_b)
            slug_sim = difflib.SequenceMatcher(None, a["slug"], b["slug"]).ratio()
            title_sim = difflib.SequenceMatcher(None, a["title"], b["title"]).ratio()
            if overlap or slug_sim >= SIMILARITY_THRESHOLD or title_sim >= SIMILARITY_THRESHOLD:
                score = overlap * 10 + max(slug_sim, title_sim)
                candidates.append((score, key, a, b))
    candidates.sort(key=lambda c: -c[0])
    return candidates[:MAX_DEDUP_PAIRS]


def render_merged_body(data: dict, sources: list) -> str:
    today = datetime.date.today().isoformat()
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
    return f"""{front}

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

（暂无——交叉链接由后处理注入）

## 证据与来源

本页面证据来自 {len(sources)} 个源文件：{"、".join(f"`{s}`" for s in sources)}。

## 未确认事项

{bullets(data['uncertainties'])}
"""


def step_dedup(pages: list, state: State, llm) -> bool:
    candidates = find_dedup_candidates(pages, state)
    if not candidates:
        print("[dedup] 无新候选对（已定夺过的对不再重复送审）")
        return False
    print(f"[dedup] 候选 {len(candidates)} 对（上限 {MAX_DEDUP_PAIRS}），逐对送 LLM 定夺")
    merged_any = False
    for _, key, a, b in candidates:
        # 本轮更早的合并可能已删掉其中一页——不存在的页面不再送审
        if not a["path"].exists() or not b["path"].exists():
            continue
        allowed = {s for s, _ in a["sources"]} | {s for s, _ in b["sources"]}
        text_a = (a["front_text"] + a["body"])[:MAX_PAGE_CHARS]
        text_b = (b["front_text"] + b["body"])[:MAX_PAGE_CHARS]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": DEDUP_PROMPT.format(
                slug_a=a["slug"], slug_b=b["slug"],
                page_a=text_a, page_b=text_b)},
        ]
        data = call_structured(llm, messages,
                               lambda d: validate_judge(d, a["slug"], b["slug"], allowed),
                               f"dedup {a['slug']}|{b['slug']}")
        state.pages["dedup_checked"].append(key)   # 无论结论如何都记账：不重复送审
        if data is None:
            print(f"  [跳过] {a['slug']} | {b['slug']}：定夺调用失败，留待下次")
            state.pages["dedup_checked"].remove(key)
            continue
        if not data["merge"]:
            print(f"  [保留] {a['slug']} | {b['slug']}：{data['reason']}")
            continue

        primary = a if data["primary_slug"] == a["slug"] else b
        secondary = b if primary is a else a
        merged = data["merged"]
        srcs = sorted({kp["source_path"] for kp in merged["key_points"]} | allowed)
        out = WIKI_DIR / PAGE_TYPE_DIRS[merged["page_type"]] / f"{primary['slug']}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_merged_body(merged, srcs), encoding="utf-8", newline="\n")
        if out != primary["path"] and primary["path"].exists():
            primary["path"].unlink()               # page_type 变了 → 旧位置的文件移除
        secondary["path"].unlink()

        entry = state.pages["pages"].setdefault(primary["slug"], {})
        aliases = set(entry.get("aliases", []))
        aliases |= {secondary["title"]}
        aliases |= set(state.pages["pages"].get(secondary["slug"], {}).get("aliases", []))
        aliases.discard(merged["title"])
        entry.update({
            "title": merged["title"], "page_type": merged["page_type"],
            "path": out.relative_to(WORKSPACE).as_posix(), "sources": srcs,
            "aliases": sorted(aliases),
            "updated_at": datetime.date.today().isoformat(),
        })
        state.pages["pages"].pop(secondary["slug"], None)
        # 重定向：旧 slug → 新 slug。已有指向旧 slug 的重定向也一并指到最新目标
        state.pages["redirects"][secondary["slug"]] = primary["slug"]
        for old, target in list(state.pages["redirects"].items()):
            if target == secondary["slug"]:
                state.pages["redirects"][old] = primary["slug"]
        print(f"  [合并] {secondary['slug']} → {primary['slug']}：{data['reason']}")
        append_log(entry["path"], f"dedup 合并（{secondary['slug']} → {primary['slug']}）",
                   len(merged["uncertainties"]))
        merged_any = True
    state.save()
    return merged_any


# ─────────────────────────── 步骤 2：crosslink ───────────────────────────

def rel_href(from_page: Path, to_rel_workspace: str) -> str:
    target = WORKSPACE / to_rel_workspace
    return PurePosixPath(os.path.relpath(target, from_page.parent).replace("\\", "/")).as_posix()


def inject_inline_links(page: dict, terms: list, state: State) -> int:
    """在正文的普通文本里，把其他页面的标题/别名的**首次出现**转为链接。
    跳过：frontmatter（不在 body 里）、标题行、代码围栏、行内代码段、已链接文本。
    对照 linkifyContent 的同款规则。"""
    body = page["body"]
    existing_hrefs = {m.group(2) for m in MD_LINK_RE.finditer(body)}
    lines = body.splitlines()
    in_fence = False
    injected = 0
    linked = set()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith("#"):
            continue
        for term, slug in terms:
            if slug == page["slug"] or slug in linked:
                continue
            href = rel_href(page["path"], state.pages["pages"][slug]["path"])
            if href in existing_hrefs:
                linked.add(slug)          # 页面里已有指向该页的链接 → 幂等跳过
                continue
            if f"[{term}]" in line:
                continue
            segments = line.split("`")    # 偶数下标 = 行内代码之外的文本
            for si in range(0, len(segments), 2):
                if term in segments[si]:
                    segments[si] = segments[si].replace(term, f"[{term}]({href})", 1)
                    lines[idx] = "`".join(segments)
                    line = lines[idx]
                    linked.add(slug)
                    injected += 1
                    break
    page["body"] = "\n".join(lines)
    page["_linked_slugs"] = linked
    return injected


def step_crosslink(state: State):
    pages = all_wiki_pages()          # dedup 之后重新读盘
    known = state.pages["pages"]
    # 词表：标题 + 别名，长词优先（避免短词抢先截断长词的匹配）
    terms = []
    for slug, meta in known.items():
        for term in [meta["title"], *meta.get("aliases", [])]:
            if len(term) >= MIN_TERM_LEN:
                terms.append((term, slug))
    terms.sort(key=lambda t: -len(t[0]))

    total_injected = 0
    for page in pages:
        injected = inject_inline_links(page, terms, state)
        total_injected += injected

        # 相关页面：共享来源文件 / 共享来源目录前缀 / 本页已链接
        my_srcs = {s for s, _ in page["sources"]}
        my_prefixes = {"/".join(PurePosixPath(s).parts[:2]) for s in my_srcs}
        related = {}
        for slug, meta in known.items():
            if slug == page["slug"]:
                continue
            other_srcs = set(meta.get("sources", []))
            other_prefixes = {"/".join(PurePosixPath(s).parts[:2]) for s in other_srcs}
            if my_srcs & other_srcs:
                related[slug] = "共享来源文件"
            elif my_prefixes & other_prefixes:
                related[slug] = "共享来源目录"
            elif slug in page.get("_linked_slugs", set()):
                related[slug] = "正文提及"
        if related:
            content = "\n".join(
                f'- [{known[slug]["title"]}]({rel_href(page["path"], known[slug]["path"])})'
                f"（{reason}）"
                for slug, reason in sorted(related.items()))
        else:
            content = "（暂无相关页面）"
        page["body"] = section_replace(page["body"], "相关页面", content)
        write_page_text(page)
        # 链接图落入 pages.json（s06 lint 会用）
        known[page["slug"]]["links"] = sorted(
            set(related) | page.get("_linked_slugs", set()))
    state.save()
    print(f"[crosslink] 内联链接注入 {total_injected} 处，全部页面「相关页面」小节已重建")


# ─────────────────────────── 步骤 3：deadlink ───────────────────────────

def step_deadlink(state: State):
    pages = all_wiki_pages()
    known = state.pages["pages"]
    redirects = state.pages["redirects"]
    rewritten = unlinked = 0
    for page in pages:
        body = page["body"]
        changed = False
        for m in list(MD_LINK_RE.finditer(body)):
            text, href = m.group(1), m.group(2)
            target = (page["path"].parent / href).resolve()
            if target.exists():
                continue
            slug = Path(href).stem
            final = redirects.get(slug)
            if final and final in known:
                new_href = rel_href(page["path"], known[final]["path"])
                body = body.replace(m.group(0), f"[{text}]({new_href})", 1)
                rewritten += 1
                print(f"  [重写] {page['slug']}: {href} → {new_href}（重定向 {slug} → {final}）")
            else:
                body = body.replace(m.group(0), text, 1)   # 解除链接，保留文字
                unlinked += 1
                print(f"  [解除] {page['slug']}: {href} 目标不存在，还原为纯文本")
            changed = True
        if changed:
            page["body"] = body
            write_page_text(page)
            append_log(page["path"].relative_to(WORKSPACE).as_posix(),
                       f"deadlink 清理（重写 {rewritten}，解除 {unlinked}）")
    print(f"[deadlink] 重写 {rewritten} 条（指向已合并页面），解除 {unlinked} 条（目标不存在）")


# ─────────────────────────── 步骤 4：index ───────────────────────────

def first_sentence(body: str) -> str:
    m = re.search(r"## 概述\n+(.+?)(?=\n## |\Z)", body, re.DOTALL)
    if not m:
        return ""
    text = m.group(1).strip().replace("\n", " ")
    period = text.find("。")
    sentence = text[:period + 1] if period != -1 else text
    return sentence[:90]


def step_index(state: State, llm):
    pages = all_wiki_pages()
    known = state.pages["pages"]
    groups = {}
    for page in pages:
        groups.setdefault(page["page_type"], []).append(page)

    toc_lines = []
    for ptype in PAGE_TYPE_DIRS:
        if ptype not in groups:
            continue
        toc_lines.append(f"\n## {ptype}\n")
        for page in sorted(groups[ptype], key=lambda p: p["slug"]):
            href = f"{PAGE_TYPE_DIRS[ptype]}/{page['slug']}.md"
            desc = first_sentence(page["body"])
            toc_lines.append(f"- [{page['title']}]({href}) — {desc}")
    toc = "\n".join(toc_lines)

    # 导读缓存：页面集合（slug+标题+更新时间）没变就不再调 LLM——幂等重跑不重复付费
    pages_hash = hashlib.sha256(json.dumps(
        sorted((p["slug"], p["title"], known.get(p["slug"], {}).get("updated_at", ""))
               for p in pages), ensure_ascii=False).encode()).hexdigest()[:16]
    cache = state.pages["index_cache"]
    if cache.get("pages_hash") == pages_hash:
        intro = cache["intro"]
        print("[index] 页面集合未变化，导读用缓存（不调 LLM）")
    else:
        data = call_structured(llm, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": INDEX_INTRO_PROMPT.format(toc=toc)},
        ], validate_intro, "index 导读")
        intro = data["intro"] if data else "（导读生成失败，此处为占位。）"
        state.pages["index_cache"] = {"pages_hash": pages_hash, "intro": intro}

    today = datetime.date.today().isoformat()
    index_text = f"""# Wiki 索引

> 更新于 {today}，共 {len(pages)} 个页面。本索引由后处理流水线自动重建，请勿手工编辑。

{intro}
{toc}
"""
    (WIKI_DIR / "index.md").write_text(index_text, encoding="utf-8", newline="\n")
    state.save()
    append_log("wiki/index.md", f"index 重建（{len(pages)} 个页面）")
    print(f"[index] 重建完成：{len(pages)} 个页面，{len(groups)} 个分组")


# ─────────────────────────── 入口 ───────────────────────────

def cmd_postprocess(mock: bool):
    llm = MockLLM() if mock else OpenAILLM(load_env())
    state = State()
    pages = step_discover(state)
    if not pages:
        sys.exit("wiki/ 下没有页面。先运行 s03 的 ingest 生成页面。")
    step_dedup(pages, state, llm)
    step_crosslink(state)      # dedup 可能改动/删除页面，内部会重新读盘
    step_deadlink(state)
    step_index(state, llm)
    state.save()
    print("\n[完成] 后处理流水线结束（dedup → crosslink → deadlink → index）")


def main():
    parser = argparse.ArgumentParser(description="s04：Wiki 后处理流水线")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("postprocess", help="去重 → 交叉链接 → 死链清理 → 重建索引")
    p.add_argument("--mock", action="store_true", help="使用 Mock LLM")
    args = parser.parse_args()
    cmd_postprocess(args.mock)


if __name__ == "__main__":
    main()
