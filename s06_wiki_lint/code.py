# -*- coding: utf-8 -*-
"""
s06：Wiki Lint

流水线篇的收官：一个**完全不调 LLM** 的质量检查器。

  python code.py lint
    ├─ 解析全部页面（frontmatter + 正文 + 链接）
    ├─ 九条确定性规则逐一检查（见下表）
    ├─ 控制台分级报告（error / warning / info）
    └─ 结构化报告写入 state/issues.json —— **只报告，不修改**

为什么不调 LLM？因为这九条规则的判定标准都是**机械可验证**的：链接目标存在不存在、
引用的文件在不在、行号超没超范围——不需要判断力，只需要检查。需要判断力的部分
（这个问题该怎么修？页面还有没有救？）是 s12 Fixer Agent 的工作，它消费的正是
本阶段产出的 issues.json。这就是双范式的交接点：**流水线负责发现，Agent 负责定夺**。

规则表（命名尽量对齐 wiki_lint.go 的 WikiLintIssueType）：
  broken_link       error    页面里的 .md 链接目标不存在
  dead_source_ref   error    引用的源文件在项目里不存在（对照 stale_ref）
  cite_out_of_range warning  引用行号超出源文件实际行数（文件被改短了）
  missing_sources   warning  非 deprecated 页面没有任何来源（对照 empty_content）
  title_conflict    warning  两个页面标题/别名撞车（对照 duplicate_slug）
  index_missing     warning  页面存在但 index.md 未收录
  stale_draft       info     draft 状态超过 N 天没更新
  orphan_page       info     没有任何其他页面链接到它（对照 orphan_page）
  missing_backlink  info     A 链到 B 但 B 没链回 A（对照 missing_cross_ref）

issues.json 的幂等合并：issue 的稳定键 = (type, slug, evidence)。重跑 lint 时——
仍然检出的 issue 保留原 id / status / created_at（Agent 标过的状态不丢）；
不再检出的自动标 auto_resolved；新检出的分配新 id。

对照 WeKnora：
  - wiki_lint.go：WikiLintService.RunLint 同样是纯确定性检查 + 分级报告；
    它还有 AutoFix（机械可修的直接修），教学版把「修」全部留给 s12
  - wiki_flag_issue（tools/wiki_flag_issue.go）：Agent 侧手动标记问题的工具，
    结构 {slug, issue_type, description, status: "pending"}——教学版 issues.json
    的条目结构与之对齐，s12 的 Fixer 同时消费两个来源的问题

用法：
    python code.py lint [--stale-days N] [--strict]
    （--strict：warning 也导致非零退出码，适合接 CI）
"""

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

# ─────────────────────────── 路径与常量 ───────────────────────────

SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", r"C:\Desktop\Project\WeKnora"))
WORKSPACE = Path(os.environ.get(
    "WIKI_WORKSPACE", Path(__file__).resolve().parent.parent / "workspace"))
WIKI_DIR = WORKSPACE / "wiki"
STATE_DIR = WORKSPACE / "state"

STALE_DAYS_DEFAULT = 14

PAGE_TYPE_DIRS = ["architecture", "modules", "workflows", "api", "data",
                  "infrastructure", "decisions", "glossary"]

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)#\s]+\.md)\)")
SOURCE_LINE_RE = re.compile(r"-\s*\{path:\s*([^,}]+),\s*lines:\s*([^}]+)\}")
CITE_RE = re.compile(r"（来源：`([^:`]+):([\d\-]+)`）")
LINES_RE = re.compile(r"^(\d+)(?:-(\d+))?$")

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def load_json(path: Path, default):
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ─────────────────────────── 页面解析 ───────────────────────────

def parse_page(path: Path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    front_text, body = parts[1], parts[2]
    front = {}
    for line in front_text.splitlines():
        if ":" in line and not line.strip().startswith("-"):
            key, _, value = line.partition(":")
            front[key.strip()] = value.strip()
    sources = [(m.group(1).strip(), m.group(2).strip())
               for m in SOURCE_LINE_RE.finditer(front_text)]
    return {
        "path": path,
        "rel": path.relative_to(WORKSPACE).as_posix(),
        "slug": path.stem,
        "title": front.get("title", path.stem),
        "status": front.get("status", "draft"),
        "updated_at": front.get("updated_at", ""),
        "sources": sources,
        "body": body,
        "links": [(m.group(1), m.group(2)) for m in MD_LINK_RE.finditer(body)],
        "cites": [(m.group(1), m.group(2)) for m in CITE_RE.finditer(body)],
    }


def collect_pages():
    pages = []
    for sub in PAGE_TYPE_DIRS:
        d = WIKI_DIR / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            p = parse_page(f)
            if p:
                pages.append(p)
    return pages


def resolve_link(page, href: str):
    """把页面里的相对链接解析成目标 slug；解析不到（目标不存在）返回 (None, 绝对路径)。"""
    target = (page["path"].parent / href).resolve()
    if target.exists():
        return target.stem, target
    return None, target


# ─────────────────────────── 九条规则 ───────────────────────────
# 每条规则一个函数，输入解析好的上下文，输出 issue dict 列表。
# issue 的字段结构对齐 wiki_flag_issue：slug / issue_type / description + 补充字段。

def make_issue(itype, severity, slug, page_rel, description, evidence):
    return {"type": itype, "severity": severity, "slug": slug, "page": page_rel,
            "description": description, "evidence": evidence}


def rule_broken_link(ctx):
    issues = []
    for page in ctx["pages"] + [ctx["index"]] if ctx["index"] else ctx["pages"]:
        if page is None:
            continue
        for text, href in page["links"]:
            slug, target = resolve_link(page, href)
            if slug is None:
                issues.append(make_issue(
                    "broken_link", "error", page["slug"], page["rel"],
                    f"链接「{text}」指向不存在的页面", f"{href}"))
    return issues


def rule_dead_source_ref(ctx):
    """frontmatter 来源 + 正文行号引用，两处都查文件是否存在。"""
    issues = []
    for page in ctx["pages"]:
        seen = set()
        for src, lines in page["sources"] + page["cites"]:
            if src in seen:
                continue
            seen.add(src)
            if not (SOURCE_ROOT / src).is_file():
                issues.append(make_issue(
                    "dead_source_ref", "error", page["slug"], page["rel"],
                    f"引用的源文件在项目中不存在", src))
    return issues


def rule_cite_out_of_range(ctx):
    """引用的行号超出源文件实际行数——文件被改短了，引用已经指到虚空。
    这是 s05『剥除来源』覆盖不到的情形：文件还在，但行没了。"""
    issues = []
    line_counts = {}
    for page in ctx["pages"]:
        flagged = set()
        for src, lines in page["sources"] + page["cites"]:
            fpath = SOURCE_ROOT / src
            if not fpath.is_file():
                continue                      # 文件不存在归 dead_source_ref 管
            if src not in line_counts:
                line_counts[src] = fpath.read_text(
                    encoding="utf-8", errors="replace").count("\n") + 1
            m = LINES_RE.match(lines.strip())
            if not m:
                continue
            end = int(m.group(2) or m.group(1))
            if end > line_counts[src] and (src, lines) not in flagged:
                flagged.add((src, lines))
                issues.append(make_issue(
                    "cite_out_of_range", "warning", page["slug"], page["rel"],
                    f"引用行号超出文件实际行数（{line_counts[src]} 行）",
                    f"{src}:{lines}"))
    return issues


def rule_missing_sources(ctx):
    issues = []
    for page in ctx["pages"]:
        if page["status"] == "deprecated":
            continue                          # 已废弃页面本来就没有存活来源
        if not page["sources"]:
            issues.append(make_issue(
                "missing_sources", "warning", page["slug"], page["rel"],
                "页面没有任何来源引用，事实无法验证", "frontmatter sources 为空"))
    return issues


def rule_title_conflict(ctx):
    by_title = {}
    for page in ctx["pages"]:
        by_title.setdefault(page["title"], []).append(page["slug"])
    # 别名也参与撞车检查（别名是 s04 合并留下的「曾用名」）
    known = ctx["state_pages"]
    for slug, meta in known.items():
        for alias in meta.get("aliases", []):
            by_title.setdefault(alias, []).append(f"{slug}(alias)")
    issues = []
    for title, slugs in by_title.items():
        if len(slugs) > 1:
            issues.append(make_issue(
                "title_conflict", "warning", slugs[0].split("(")[0], "",
                f"标题/别名「{title}」被多个页面使用", ", ".join(slugs)))
    return issues


def rule_index_missing(ctx):
    if ctx["index"] is None:
        return [make_issue("index_missing", "warning", "index", "wiki/index.md",
                           "index.md 不存在（先运行 s04 postprocess）", "")]
    indexed = set()
    for _, href in ctx["index"]["links"]:
        slug, target = resolve_link(ctx["index"], href)
        if slug:
            indexed.add(slug)
    issues = []
    for page in ctx["pages"]:
        if page["slug"] not in indexed:
            issues.append(make_issue(
                "index_missing", "warning", page["slug"], page["rel"],
                "页面未被 index.md 收录", ""))
    return issues


def rule_stale_draft(ctx):
    issues = []
    today = datetime.date.today()
    for page in ctx["pages"]:
        if page["status"] != "draft":
            continue
        try:
            updated = datetime.date.fromisoformat(page["updated_at"])
        except ValueError:
            continue
        age = (today - updated).days
        if age >= ctx["stale_days"]:
            issues.append(make_issue(
                "stale_draft", "info", page["slug"], page["rel"],
                f"draft 状态已持续 {age} 天未更新（阈值 {ctx['stale_days']} 天）",
                f"updated_at: {page['updated_at']}"))
    return issues


def rule_orphan_page(ctx):
    issues = []
    for page in ctx["pages"]:
        if not ctx["incoming"].get(page["slug"]):
            issues.append(make_issue(
                "orphan_page", "info", page["slug"], page["rel"],
                "没有任何其他页面链接到本页（仅 index 可达或完全孤立）", ""))
    return issues


def rule_missing_backlink(ctx):
    issues = []
    outgoing = ctx["outgoing"]
    for src_slug, targets in outgoing.items():
        for dst in targets:
            if dst in outgoing and src_slug not in outgoing[dst]:
                issues.append(make_issue(
                    "missing_backlink", "info", dst, "",
                    f"{src_slug} 链接了 {dst}，但 {dst} 没有链回",
                    f"{src_slug} → {dst}"))
    return issues


RULES = [rule_broken_link, rule_dead_source_ref, rule_cite_out_of_range,
         rule_missing_sources, rule_title_conflict, rule_index_missing,
         rule_stale_draft, rule_orphan_page, rule_missing_backlink]


# ─────────────────────────── issues.json 幂等合并 ───────────────────────────

def issue_key(issue) -> str:
    return f'{issue["type"]}|{issue["slug"]}|{issue["evidence"]}'


def merge_issues(found: list):
    """稳定键合并：保留已有 issue 的 id/status/created_at（Agent 标过的不丢），
    消失的标 auto_resolved，新增的分配新 id。"""
    store = load_json(STATE_DIR / "issues.json", {"next_id": 1, "issues": []})
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    existing = {issue_key(i): i for i in store["issues"]}
    found_keys = set()
    merged, new_count = [], 0

    for issue in found:
        key = issue_key(issue)
        found_keys.add(key)
        if key in existing:
            old = existing[key]
            old.update({k: issue[k] for k in ("severity", "page", "description")})
            if old["status"] == "auto_resolved":   # 问题回归了：重新打开
                old["status"] = "open"
                old["updated_at"] = now
            merged.append(old)
        else:
            merged.append({"id": store["next_id"], **issue, "status": "open",
                           "detected_by": "lint", "created_at": now, "updated_at": now})
            store["next_id"] += 1
            new_count += 1

    resolved_count = 0
    for key, old in existing.items():
        if key not in found_keys:
            if old["status"] == "open":
                old["status"] = "auto_resolved"
                old["updated_at"] = now
                resolved_count += 1
            merged.append(old)                     # 历史保留（含人工标记的状态）

    store["issues"] = merged
    store["last_lint"] = now
    save_json(STATE_DIR / "issues.json", store)
    return store, new_count, resolved_count


# ─────────────────────────── lint 主流程 ───────────────────────────

def cmd_lint(stale_days: int, strict: bool):
    pages = collect_pages()
    if not pages:
        sys.exit("wiki/ 下没有页面。先运行 s03 ingest / s04 postprocess。")
    index_path = WIKI_DIR / "index.md"
    index = None
    if index_path.is_file():
        text = index_path.read_text(encoding="utf-8")
        index = {"path": index_path, "rel": "wiki/index.md", "slug": "index",
                 "links": [(m.group(1), m.group(2)) for m in MD_LINK_RE.finditer(text)]}

    # 链接图：outgoing[slug] = 它链接到的 slug 集合；incoming 反向（不含 index）
    outgoing, incoming = {}, {}
    for page in pages:
        targets = set()
        for _, href in page["links"]:
            slug, _t = resolve_link(page, href)
            if slug and slug != page["slug"]:
                targets.add(slug)
        outgoing[page["slug"]] = targets
        for dst in targets:
            incoming.setdefault(dst, set()).add(page["slug"])

    state_pages = load_json(STATE_DIR / "pages.json", {"pages": {}})["pages"]
    ctx = {"pages": pages, "index": index, "outgoing": outgoing,
           "incoming": incoming, "state_pages": state_pages,
           "stale_days": stale_days}

    found = []
    for rule in RULES:
        found.extend(rule(ctx))
    found.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], i["type"], i["slug"]))

    total_cites = sum(len(p["sources"]) + len(p["cites"]) for p in pages)
    print(f"[lint] 页面 {len(pages)} 个（另有 index），来源/引用 {total_cites} 处，"
          f"规则 {len(RULES)} 条")

    store, new_count, resolved_count = merge_issues(found)

    by_sev = {}
    for issue in found:
        by_sev.setdefault(issue["severity"], []).append(issue)
    for sev in ("error", "warning", "info"):
        group = by_sev.get(sev, [])
        if not group:
            continue
        print(f"\n=== {sev} ({len(group)}) ===")
        for issue in group:
            stored = next(i for i in store["issues"] if issue_key(i) == issue_key(issue))
            loc = f" [{issue['page']}]" if issue["page"] else ""
            ev = f"（{issue['evidence']}）" if issue["evidence"] else ""
            print(f"  #{stored['id']} {issue['type']} {issue['slug']}{loc}: "
                  f"{issue['description']}{ev}")

    open_count = sum(1 for i in store["issues"] if i["status"] == "open")
    print(f"\n[issues.json] open {open_count}（新增 {new_count}），"
          f"本轮 auto_resolved {resolved_count}，历史共 {len(store['issues'])} 条")
    print("[提示] 修复由 s12 的 Fixer Agent 消费 issues.json 逐条判断——lint 只报告不动手")

    errors = len(by_sev.get("error", []))
    warnings = len(by_sev.get("warning", []))
    if errors or (strict and warnings):
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="s06：Wiki Lint（纯确定性，不调 LLM）")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("lint", help="检查 Wiki 质量，报告写入 state/issues.json")
    p.add_argument("--stale-days", type=int, default=STALE_DAYS_DEFAULT,
                   help=f"draft 超过多少天算 stale（默认 {STALE_DAYS_DEFAULT}）")
    p.add_argument("--strict", action="store_true",
                   help="warning 也导致非零退出码（CI 模式）")
    args = parser.parse_args()
    cmd_lint(args.stale_days, args.strict)


if __name__ == "__main__":
    main()
