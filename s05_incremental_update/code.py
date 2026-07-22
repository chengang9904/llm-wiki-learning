# -*- coding: utf-8 -*-
"""
s05：增量更新

s03 的 ingest 靠「全量扫描 + 哈希对比」找活干——正确，但每次都要遍历整个项目。
s05 引入 git 作为变更信号源，把「找出变化」的成本从 O(项目) 降到 O(变更)：

  python code.py update
    ├─ 变更检测（二选一，自动降级）
    │    git 模式 ：state 记录 last_ingest_commit，
    │              git diff --name-status <commit>..HEAD → 新增/修改/删除
    │    哈希模式 ：非 git 目录或首次运行的 fallback——全量扫描对比 manifest
    ├─ 新增/修改 → 入队重跑 map-reduce（**重新读源文件**，绝不只凭旧摘要更新）
    ├─ 删除     → 不粗暴删页面：
    │              页面还有其他来源 → 确定性剥除引用该文件的事实与来源行
    │              页面失去全部来源 → 标 status: deprecated（保留文件供人复核）
    └─ 成功后记录新的 last_ingest_commit + 写变更日志

对照 WeKnora：
  - 触发机制：WeKnora 由文档上传/删除事件驱动，且带防抖（wikiIngestDelay = 30s，
    wiki_ingest.go:97）——30 秒内的连续上传攒成一批再处理。教学版是手动命令，
    用 git diff 替代事件触发，天然自带「攒批」效果（两次 update 之间的全部提交一起处理）。
  - task_pending_ops：变更仍然走 s03 的 op 队列，幂等/失败隔离/断点续跑全部保留。
  - 删除处理：markKnowledgeDeletedForWiki（knowledge_delete.go:337）写「墓碑」防止
    在途任务给已删文档建页；sanitizeDeadSummaryLinks（wiki_ingest.go:1302）清理
    指向已删来源的摘要链接。教学版单进程无在途任务，直接做来源剥除与 deprecated 标记。

用法：
    python code.py update [--mock] [--path 前缀] [--limit N]
    python code.py status
    python code.py retry
环境变量：SOURCE_ROOT（被分析项目）、WIKI_WORKSPACE（workspace 位置，便于隔离演示）
"""

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ─────────────────────────── 路径与常量 ───────────────────────────

SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", r"C:\Desktop\Project\WeKnora"))
WORKSPACE = Path(os.environ.get(
    "WIKI_WORKSPACE", Path(__file__).resolve().parent.parent / "workspace"))
STATE_DIR = WORKSPACE / "state"

MAX_CONTENT_CHARS = 32768
MAX_RETRIES = 2
BATCH_SIZE = 5
MAX_FAIL_RETRIES = 2
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


def passes_filters(rel: str) -> bool:
    """git diff 给出的路径也必须过同一套排除规则——变更信号不豁免安全红线。"""
    p = Path(rel)
    if set(p.parts) & EXCLUDED_DIRS:
        return False
    if is_sensitive_path(p) or p.suffix.lower() not in INCLUDE_EXTS:
        return False
    if any(fnmatch.fnmatch(p.name, pat) for pat in EXCLUDED_FILE_PATTERNS):
        return False
    return True


def scan_sources(root: Path):
    included = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(root).as_posix()
            if passes_filters(rel):
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


# ─────────────────────────── 状态（同 s03，sources.json 多了 last_ingest_commit） ───────────────────────────

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
    def __init__(self):
        self.sources = load_json(STATE_DIR / "sources.json", {"files": {}})
        self.pending = load_json(STATE_DIR / "pending.json", {"ops": [], "next_id": 1})
        self.pages = load_json(STATE_DIR / "pages.json", {"pages": {}})
        self.pages.setdefault("redirects", {})

    def save(self):
        save_json(STATE_DIR / "sources.json", self.sources)
        save_json(STATE_DIR / "pending.json", self.pending)
        save_json(STATE_DIR / "pages.json", self.pages)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def append_log(page_rel: str, reason: str, uncertainties: int = 0):
    log_path = WORKSPACE / "logs" / "wiki-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        if f.tell() == 0:
            f.write("# Wiki 修改日志\n\n")
        f.write(f"- {now} | {page_rel} | 原因: {reason} | 未确认事项: {uncertainties}\n")


# ─────────────────────────── git 变更检测 ───────────────────────────

def run_git(root: Path, *args):
    """跑一条 git 命令，失败返回 None（git 不存在 / 非仓库 / commit 无效……
    所有失败都走同一条路：降级到哈希模式）。"""
    try:
        result = subprocess.run(["git", "-C", str(root), *args],
                                capture_output=True, text=True, timeout=30,
                                encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def detect_changes(state: State, path_filter: str):
    """返回 (added_modified 相对路径列表, deleted 相对路径列表, 模式说明)。

    git 模式：git diff --name-status <last_ingest_commit>..HEAD
    哈希模式：全量扫描对比 manifest（首次运行 / 非 git 目录 / commit 失效时的 fallback）
    """
    prefix = (path_filter.replace("\\", "/").rstrip("/") + "/") if path_filter else ""

    since = state.sources.get("last_ingest_commit")
    head = run_git(SOURCE_ROOT, "rev-parse", "HEAD")
    if since and head:
        diff = run_git(SOURCE_ROOT, "diff", "--name-status", "--no-renames",
                       f"{since}..HEAD")
        if diff is not None:
            added_modified, deleted = [], []
            for line in diff.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                status, rel = parts[0], parts[-1]
                if not passes_filters(rel) or (prefix and not rel.startswith(prefix)):
                    continue
                if status.startswith("D"):
                    deleted.append(rel)
                else:                      # A / M（--no-renames 下没有 R）
                    added_modified.append(rel)
            return added_modified, deleted, f"git diff {since[:8]}..HEAD"

    # 哈希模式 fallback：磁盘是唯一事实
    included = scan_sources(SOURCE_ROOT)
    if prefix:
        included = [f for f in included
                    if f.relative_to(SOURCE_ROOT).as_posix().startswith(prefix)]
    on_disk = set()
    added_modified = []
    for fpath in included:
        rel = fpath.relative_to(SOURCE_ROOT).as_posix()
        on_disk.add(rel)
        entry = state.sources["files"].get(rel, {})
        if entry.get("processed_hash") != file_hash(fpath):
            added_modified.append(rel)
    deleted = [rel for rel in state.sources["files"]
               if (not prefix or rel.startswith(prefix)) and rel not in on_disk]
    return added_modified, deleted, "哈希对比（无 last_ingest_commit 或非 git 目录）"


# ─────────────────────────── 删除处理：剥除来源 / 标记 deprecated ───────────────────────────

def parse_page_file(path: Path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return {"front": parts[1], "body": parts[2].lstrip("\n"), "path": path}


def write_page_file(page):
    page["path"].write_text("---" + page["front"] + "---\n\n" + page["body"],
                            encoding="utf-8", newline="\n")


def handle_deleted_source(rel: str, state: State):
    """一个源文件被删除。它贡献过的每个页面：
    还有其他来源 → 确定性剥除该来源的事实/来源行，并在未确认事项里留痕；
    失去全部来源 → 标 deprecated，保留文件供人（或 s12 的 Fixer）复核。
    全程不调 LLM——这是结构可预知的清理，流水线的活。"""
    today = datetime.date.today().isoformat()
    for slug, meta in list(state.pages["pages"].items()):
        if rel not in meta.get("sources", []):
            continue
        page_path = WORKSPACE / meta["path"]
        page = parse_page_file(page_path) if page_path.is_file() else None
        if page is None:
            meta["sources"] = [s for s in meta["sources"] if s != rel]
            continue

        remaining = [s for s in meta["sources"] if s != rel]
        # frontmatter：剥除该来源的 sources 行；正文：剥除引用该来源的事实条目
        page["front"] = "\n".join(
            ln for ln in page["front"].splitlines()
            if f"path: {rel}," not in ln) + "\n"
        stripped = 0
        kept_lines = []
        for ln in page["body"].splitlines():
            if f"（来源：`{rel}:" in ln:
                stripped += 1
                continue
            kept_lines.append(ln)
        page["body"] = "\n".join(kept_lines)
        note = f"来源 {rel} 已于 {today} 从项目中删除，引用它的 {stripped} 条事实已移除"

        if remaining:
            meta["sources"] = remaining
            meta["updated_at"] = today
            page["body"] = _append_uncertainty(page["body"], note + "，本页面建议复核")
            page["front"] = re.sub(r"^updated_at: .*$", f"updated_at: {today}",
                                   page["front"], flags=re.MULTILINE)
            write_page_file(page)
            print(f"  [剥除] {slug}：移除来源 {rel}（{stripped} 条事实），"
                  f"剩余 {len(remaining)} 个来源")
            append_log(meta["path"], f"来源删除清理（{rel}，剥除 {stripped} 条事实）", 1)
        else:
            meta["sources"] = []
            meta["status"] = "deprecated"
            meta["updated_at"] = today
            page["front"] = re.sub(r"^status: .*$", "status: deprecated",
                                   page["front"], flags=re.MULTILINE)
            page["front"] = re.sub(r"^updated_at: .*$", f"updated_at: {today}",
                                   page["front"], flags=re.MULTILINE)
            page["body"] = _append_uncertainty(
                page["body"], note + "。页面已无任何存活来源，标记为 deprecated")
            write_page_file(page)
            print(f"  [废弃] {slug}：全部来源已删除，标记 status: deprecated（文件保留）")
            append_log(meta["path"], f"标记 deprecated（最后来源 {rel} 已删除）", 1)

    # manifest 与队列的清理
    state.sources["files"].pop(rel, None)
    state.pending["ops"] = [op for op in state.pending["ops"]
                            if not (op["path"] == rel and op["status"] == "pending")]


def _append_uncertainty(body: str, note: str) -> str:
    pattern = re.compile(r"(^## 未确认事项\n)(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    m = pattern.search(body)
    if not m:
        return body.rstrip() + f"\n\n## 未确认事项\n\n- {note}\n"
    content = m.group(2).rstrip()
    if content.strip() in ("", "（无）"):
        content = f"\n- {note}"
    else:
        content += f"\n- {note}"
    return pattern.sub(lambda _: m.group(1) + content + "\n\n", body, count=1)


# ─────────────────────────── Prompt / 校验 / LLM（同 s03） ───────────────────────────

SYSTEM_PROMPT = """你是一个代码分析器，为软件项目的 Wiki 生成页面素材。

### JSON Formatting Rules
- 只输出 JSON 对象本身，不要任何前言、解释或 Markdown 代码围栏。
- 字符串值内不要使用裸换行符；确需换行用 \\n 转义。
- 所有字段都必须出现，即使为空数组。

### 引用纪律
- 每条事实都必须带真实来源（行号 / 文件路径），不得发明来源。
- 没有把握的事实写进 uncertainties，不要写进正文字段。"""

MAP_PROMPT = """分析下面这个源文件（每行行首标了行号），把它的内容归入 1-3 个 Wiki 主题。
返回 JSON 对象：{{"topics": [主题更新, ...]}}，每个主题更新的字段：

- "slug": 字符串。主题页面的标识，小写字母/数字/连字符。**优先复用已有 slug**。
- "title": 字符串。主题标题，中文名词短语。
- "page_type": architecture | module | workflow | api | data | infrastructure |
  decision | glossary 之一。
- "summary": 字符串。本文件对这个主题贡献了什么信息，2-4 句话。
- "responsibilities": 字符串数组，1-5 条。
- "facts": 对象数组，1-6 条：{{"point": "一句话事实", "lines": "起-止行号"}}。
- "relations": 字符串数组，可为空。
- "uncertainties": 字符串数组，可为空。

已有页面 slug（优先复用）：{existing_slugs}

文件路径：{path}

文件内容（带行号，可能已截断）：
<file_content>
{content}
</file_content>"""

REDUCE_PROMPT = """你在维护 Wiki 页面「{slug}」。把新证据归并进旧页面，输出归并后的完整页面数据。

规则：
- 保留旧页面中仍然成立的事实，合并新证据，去掉重复；
- **新证据来自源文件的最新版本**：同一来源文件的旧事实若与新证据冲突，以新证据为准；
- 每条 key_points 必须带 source_path 和 lines，只能来自旧页面或新证据中出现过的来源；
- 矛盾点记入 uncertainties。

返回 JSON 对象：
- "title" / "page_type" / "summary"
- "responsibilities": 字符串数组，1-8 条
- "key_points": 对象数组，1-40 条：{{"point": "...", "source_path": "...", "lines": "起-止"}}
- "call_relations" / "uncertainties": 字符串数组，可为空

<old_page>
{old_page}
</old_page>

<evidence>
{evidence}
</evidence>"""


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
            errors.append(f'topics[{ti}].slug 必须匹配 [a-z0-9-]')
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
            if not isinstance(fact, dict) or not str(fact.get("point", "")).strip():
                errors.append(f"topics[{ti}].facts[{fi}] 必须是含非空 point 的对象")
                continue
            m = LINES_RE.match(str(fact.get("lines", "")))
            if not m:
                errors.append(f'topics[{ti}].facts[{fi}].lines 格式错误')
                continue
            start, end = int(m.group(1)), int(m.group(2) or m.group(1))
            if start < 1 or end > total_lines or start > end:
                errors.append(f"topics[{ti}].facts[{fi}].lines 超出范围 1-{total_lines}")
    return errors


def validate_reduce(data, allowed_paths: set) -> list:
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
        if not isinstance(kp, dict) or not str(kp.get("point", "")).strip():
            errors.append(f"key_points[{i}] 必须是含非空 point 的对象")
            continue
        if kp.get("source_path") not in allowed_paths:
            errors.append(f'key_points[{i}].source_path 不在允许的来源集合内')
        if not LINES_RE.match(str(kp.get("lines", ""))):
            errors.append(f'key_points[{i}].lines 格式错误')
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
    """同 s03：map 用正则提取真实定义行，slug 按目录导出；reduce 先读旧页面再叠加新证据。"""

    DEF_RE = re.compile(r"^\s*(\d+)\|\s*(?:func|def|type|class)\s+(?:\([^)]*\)\s*)?(\w+)",
                        re.MULTILINE)
    OLD_POINT_RE = re.compile(r"^- (.+?)（来源：`([^:`]+):([\d\-]+)`）", re.MULTILINE)

    def complete(self, messages: list) -> str:
        prompt = next((m["content"] for m in messages
                       if m["role"] == "user"
                       and ("<file_content>" in m["content"] or "<evidence>" in m["content"])),
                      messages[-1]["content"])
        if "<evidence>" in prompt:
            return self._reduce(prompt)
        return self._map(prompt)

    def _map(self, prompt: str) -> str:
        path = re.search(r"文件路径：(.+)", prompt).group(1).strip()
        content = prompt.split("<file_content>")[1].split("</file_content>")[0]
        rel = Path(path)
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

    def _reduce(self, prompt: str) -> str:
        evidence = json.loads(prompt.split("<evidence>")[1].split("</evidence>")[0])
        old_page = prompt.split("<old_page>")[1].split("</old_page>")[0]
        new_paths = {u["source_path"] for u in evidence}
        key_points, resp, unc, seen = [], [], [], set()
        # 读-改-写的「读」：抬入旧事实——但**同一来源文件被本批重新 map 过**的旧事实
        # 要丢弃（以新证据为准），这正是「修改文件 → 页面跟着变新」的机制
        for point, path, lines in self.OLD_POINT_RE.findall(old_page):
            if path in new_paths:
                continue
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
            "title": evidence[0]["title"],
            "page_type": evidence[0]["page_type"],
            "summary": "；".join(dict.fromkeys(u["summary"] for u in evidence)),
            "responsibilities": resp or ["（mock 未能提取职责）"],
            "key_points": key_points[:40],
            "call_relations": [],
            "uncertainties": unc,
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


# ─────────────────────────── map / reduce（同 s03） ───────────────────────────

def number_lines(content: str) -> str:
    lines = content.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i + 1:>{width}}| {line}" for i, line in enumerate(lines))


def map_one(op: dict, llm, existing_slugs: list):
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
        topic["source_path"] = op["path"]
    return data["topics"]


def page_path_for(slug: str, page_type: str) -> Path:
    return WORKSPACE / "wiki" / PAGE_TYPE_DIRS[page_type] / f"{slug}.md"


def reduce_slug(slug: str, updates: list, llm, state: State):
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

    today = datetime.date.today().isoformat()
    sources_meta = sorted({kp["source_path"] for kp in data["key_points"]})
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

    old_entry = state.pages["pages"].get(slug, {})
    if old_entry.get("path") and old_entry["path"] != out.relative_to(WORKSPACE).as_posix():
        old_file = WORKSPACE / old_entry["path"]
        if old_file.is_file():
            old_file.unlink()
    state.pages["pages"][slug] = {
        **old_entry,
        "title": data["title"], "page_type": data["page_type"],
        "path": out.relative_to(WORKSPACE).as_posix(),
        "sources": sources_meta, "updated_at": today, "status": "draft",
    }
    append_log(out.relative_to(WORKSPACE).as_posix(),
               f"增量 reduce 归并（本批 {len(updates)} 条证据）",
               len(data["uncertainties"]))
    return True


def process_queue(state: State, llm):
    """s03 的批处理循环，原样保留幂等/失败隔离/断点续跑。"""
    while True:
        batch = [op for op in state.pending["ops"] if op["status"] == "pending"][:BATCH_SIZE]
        if not batch:
            break
        print(f"[batch] 认领 {len(batch)} 个 op")
        slug_updates, op_slugs = {}, {}
        existing_slugs = sorted(state.pages["pages"])
        for op in batch:
            print(f"  [map] #{op['id']} {op['path']}")
            topics = map_one(op, llm, existing_slugs)
            if topics is None:
                op["attempts"] += 1
                if op["attempts"] >= MAX_FAIL_RETRIES:
                    op["status"] = "failed"
                    op["error"] = f"map 重试 {op['attempts']} 次仍失败"
                state.save()
                continue
            op_slugs[op["id"]] = {t["slug"] for t in topics}
            for topic in topics:
                slug_updates.setdefault(topic["slug"], []).append(topic)

        failed_slugs = set()
        for slug, updates in sorted(slug_updates.items()):
            print(f"  [reduce] {slug} ← {len(updates)} 条更新")
            if not reduce_slug(slug, updates, llm, state):
                failed_slugs.add(slug)

        for op in batch:
            slugs = op_slugs.get(op["id"])
            if slugs is None:
                continue
            if slugs & failed_slugs:
                op["attempts"] += 1
                op["status"] = "failed" if op["attempts"] >= MAX_FAIL_RETRIES else "pending"
                op["error"] = f"reduce 失败：{', '.join(slugs & failed_slugs)}"
            else:
                op["status"] = "done"
                state.sources["files"].setdefault(op["path"], {})["processed_hash"] = op["hash"]
        state.save()


# ─────────────────────────── update 命令 ───────────────────────────

def cmd_update(args):
    llm = MockLLM() if args.mock else OpenAILLM(load_env())
    state = State()

    added_modified, deleted, mode = detect_changes(state, args.path)
    print(f"[detect] 模式：{mode}")
    print(f"[detect] 新增/修改 {len(added_modified)}，删除 {len(deleted)}")
    if not added_modified and not deleted:
        print("[完成] 没有变更，Wiki 已是最新")
        # 即便无变更也推进 commit 标记（中间可能有不影响 Wiki 的提交）
        _record_commit(state, args)
        return

    # 1) 删除处理（确定性，不调 LLM）
    if deleted:
        print(f"[delete] 处理 {len(deleted)} 个已删除的源文件")
        for rel in deleted:
            handle_deleted_source(rel, state)
        state.save()

    # 2) 新增/修改 → 入队（哈希去重）→ 跑 s03 的批处理
    if args.limit and len(added_modified) > args.limit:
        print(f"[queue] --limit {args.limit}：本次只处理前 {args.limit} 个（其余下次再跑）")
        added_modified = added_modified[:args.limit]
    existing_keys = {(op["path"], op["hash"]) for op in state.pending["ops"]
                     if op["status"] in ("pending", "failed")}
    enqueued = 0
    for rel in added_modified:
        fpath = SOURCE_ROOT / rel
        if not fpath.is_file():
            continue
        digest = file_hash(fpath)
        entry = state.sources["files"].setdefault(rel, {})
        entry["hash"], entry["mtime"] = digest, fpath.stat().st_mtime
        if entry.get("processed_hash") == digest or (rel, digest) in existing_keys:
            continue
        state.pending["ops"].append({
            "id": state.pending["next_id"], "path": rel, "hash": digest,
            "status": "pending", "attempts": 0, "error": None,
        })
        state.pending["next_id"] += 1
        enqueued += 1
    state.save()
    print(f"[queue] 入队 {enqueued} 个 op（重新读源文件做 map，绝不只凭旧摘要）")
    process_queue(state, llm)

    _record_commit(state, args)
    done = sum(1 for o in state.pending["ops"] if o["status"] == "done")
    failed = sum(1 for o in state.pending["ops"] if o["status"] == "failed")
    print(f"\n[完成] done={done} failed={failed} 页面={len(state.pages['pages'])}")
    print("[提示] 页面集合有变化时建议重跑 s04：python ..\\s04_postprocess\\code.py "
          "postprocess --mock（更新链接与索引）")


def _record_commit(state: State, args):
    """把 last_ingest_commit 推进到 HEAD。带 --path/--limit 时不推进——
    只处理了子集就宣称『已同步到 HEAD』会让下次 update 漏掉这次没处理的变更。"""
    if args.path or args.limit:
        print("[commit] 带 --path/--limit 的部分更新，不推进 last_ingest_commit")
        return
    head = run_git(SOURCE_ROOT, "rev-parse", "HEAD")
    if head:
        state.sources["last_ingest_commit"] = head
        state.save()
        print(f"[commit] last_ingest_commit → {head[:8]}")


def cmd_status():
    state = State()
    files = state.sources["files"]
    processed = sum(1 for f in files.values() if f.get("processed_hash") == f.get("hash"))
    commit = state.sources.get("last_ingest_commit", "（未记录）")
    print(f"manifest : {len(files)} 个文件，已处理 {processed}")
    print(f"commit   : last_ingest_commit = {commit[:12] if len(commit) > 12 else commit}")
    by_status = {}
    for op in state.pending["ops"]:
        by_status[op["status"]] = by_status.get(op["status"], 0) + 1
    print(f"op 队列  : {by_status or '（空）'}")
    print(f"页面     : {len(state.pages['pages'])} 个")
    for slug, meta in sorted(state.pages["pages"].items()):
        flag = "（deprecated）" if meta.get("status") == "deprecated" else ""
        print(f"  {slug} ← {len(meta.get('sources', []))} 个来源{flag}")


def cmd_retry():
    state = State()
    count = 0
    for op in state.pending["ops"]:
        if op["status"] == "failed":
            op["status"], op["attempts"], op["error"] = "pending", 0, None
            count += 1
    state.save()
    print(f"[完成] 重新入队 {count} 个 op")


def main():
    parser = argparse.ArgumentParser(description="s05：增量更新（git diff / 哈希 fallback）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_update = sub.add_parser("update", help="只对变更文件重跑 map-reduce")
    p_update.add_argument("--mock", action="store_true", help="使用 Mock LLM")
    p_update.add_argument("--path", help="只处理此相对路径前缀（部分更新，不推进 commit）")
    p_update.add_argument("--limit", type=int, default=0,
                          help="本次最多处理的文件数（0 = 不限；设置后不推进 commit）")
    sub.add_parser("status", help="查看状态（含 last_ingest_commit）")
    sub.add_parser("retry", help="把永久失败的 op 重新入队")
    args = parser.parse_args()

    if args.command == "update":
        cmd_update(args)
    elif args.command == "status":
        cmd_status()
    else:
        cmd_retry()


if __name__ == "__main__":
    main()
