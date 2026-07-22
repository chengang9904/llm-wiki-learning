# s06：Wiki Lint

## 本阶段目标

流水线篇的收官：一个**完全不调 LLM** 的质量检查器。

```
python code.py lint [--stale-days N] [--strict]
  ├─ 解析全部页面（frontmatter + 正文 + 链接 + 引用）
  ├─ 九条确定性规则
  ├─ 控制台分级报告（error / warning / info），有 error 则 exit 1
  └─ 结构化报告写入 state/issues.json —— 只报告，不修改
```

| 规则 | 级别 | 检查内容 | 对照 wiki_lint.go |
|---|---|---|---|
| `broken_link` | error | 页面里的 `.md` 链接目标不存在 | `broken_link` |
| `dead_source_ref` | error | 引用的源文件在项目里不存在 | `stale_ref` |
| `cite_out_of_range` | warning | 引用行号超出源文件实际行数 | （教学版补充） |
| `missing_sources` | warning | 非 deprecated 页面没有任何来源 | `empty_content` |
| `title_conflict` | warning | 标题/别名被多个页面使用 | `duplicate_slug` |
| `index_missing` | warning | 页面未被 index.md 收录 | — |
| `stale_draft` | info | draft 超过 N 天未更新（默认 14） | — |
| `orphan_page` | info | 没有任何其他页面链接到它 | `orphan_page` |
| `missing_backlink` | info | A 链到 B 但 B 没链回 | `missing_cross_ref` |

## 上一阶段的问题

s03–s05 让 Wiki 能生成、能归并、能跟着项目变——但**没人回答「Wiki 现在质量如何」**。
s04 的合并可能误判、s05 的剥除可能剥不干净、交叉链接可能因页面删除而失效、
页面可能永远停在 draft。这些问题散落在几十个文件里，靠人翻不现实。

## 为什么这个能力放在流水线侧（不调 LLM）

判断标准回顾（根 README 的核心问题）：**这件事的步骤能不能事先写死？**

九条规则的判定全部机械可验证：链接目标存在与否、文件在不在、行号超没超、
日期差多少天——不需要判断力，只需要检查。用 LLM 做这些是三重浪费：
贵、慢、还可能漏（模型没有「遍历所有链接」的可靠性保证）。

而 lint **发现**的问题，**修复**却往往需要判断力：断链该改写还是删除？
缺来源的页面是补引用还是整页作废？——所以 lint 只报告不动手，
issues.json 交给 s12 的 Fixer Agent 逐条定夺。
**流水线负责发现，Agent 负责定夺**——这是双范式在本课程里的正式交接点。
（WeKnora 的 `WikiLintService` 还带 `AutoFix`：机械可修的直接修，
教学版为了把交接线画清楚，把「修」全部留给 Agent 篇。）

## 新增机制

**1. 规则即函数。** 每条规则一个纯函数：输入解析好的上下文（页面、链接图、
state），输出 issue 列表。加规则 = 加函数 + 注册进 `RULES`——和 s08 工具注册表
同构，流水线和 Agent 在工程结构上是同一套味道。

**2. issues.json 的幂等合并。** issue 稳定键 = `(type, slug, evidence)`。重跑 lint：
- 仍检出 → 保留原 id / status / created_at（**Agent 或人标过的状态不丢**）；
- 不再检出 → 自动标 `auto_resolved`（历史保留，可追溯）；
- 曾 auto_resolved 又检出 → 重新 open（问题回归）；
- 新检出 → 分配新 id。

这让 lint 可以放进循环反复跑，issue 编号在多轮之间保持稳定——s12 的 Fixer
靠 id 引用问题才有意义。

**3. CI 语义的退出码。** 有 error 退出 1；`--strict` 下 warning 也算失败。
lint 因此可以挂在 CI 或 s05 update 之后当质量闸门。

## issue 数据结构（对照 wiki_flag_issue）

```json
{
  "id": 2, "type": "dead_source_ref", "severity": "error",
  "slug": "agent-overview", "page": "wiki/architecture/agent-overview.md",
  "description": "引用的源文件在项目中不存在", "evidence": "docs/agent-design.md",
  "status": "open", "detected_by": "lint",
  "created_at": "2026-07-22 02:39", "updated_at": "2026-07-22 02:39"
}
```

WeKnora 的 `wiki_flag_issue` 工具（`tools/wiki_flag_issue.go`）由 **Agent** 在问答/
巡查中手动标记问题，结构 `{slug, issue_type, description, status: "pending"}`。
教学版字段与之对齐（多了 lint 需要的 severity/evidence/稳定 id）；`detected_by`
字段预留了 `"agent"` 取值——s09/s12 的 Agent 也会往同一个 issues.json 里写，
两个来源的问题由同一个 Fixer 消费。

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版 |
|---|---|
| 九条规则函数 | `WikiLintService.RunLint`（`wiki_lint.go:94`）：同样纯确定性遍历 + 分级（`SeverityInfo/Warning/Error`，:29-31），issue 类型 `orphan_page/broken_link/stale_ref/missing_cross_ref/empty_content/duplicate_slug`（:17-22） |
| 只报告不修改 | `RunLint` 报告 + 独立的 `AutoFix`（:351）修机械问题；判断类问题同样流向 Agent（`builtin-wiki-fixer`） |
| issues.json | 出链校验靠页面存活清单（`repository/wiki_page.go:705`）；issue 落库带租户隔离 |
| 全量扫描 | 生产版按 `lintCursorBatch = 200` 分页遍历（:80）——页面可能上万，教学版一次读完 |

## 运行方法

```bash
cd C:\Desktop\Project\llm-wiki-learning\s06_wiki_lint

python code.py lint                 # 标准检查（error 时 exit 1）
python code.py lint --strict        # CI 模式：warning 也算失败
python code.py lint --stale-days 7  # 调整 draft 过期阈值

# 无需 API Key、无需 .env——本阶段不调 LLM
```

## 示例输出

```
[lint] 页面 4 个（另有 index），来源/引用 95 处，规则 9 条

=== error (2) ===
  #1 broken_link agent-overview [...]: 链接「旧版总览」指向不存在的页面（old-overview.md）
  #2 dead_source_ref agent-overview [...]: 引用的源文件在项目中不存在（docs/agent-design.md）

=== info (4) ===
  #3 missing_backlink internal-agent: agent-overview 链接了 internal-agent，但没有链回
  #4 orphan_page agent-overview [...]: 没有任何其他页面链接到本页
  #5 orphan_page cmd-server [...]: 没有任何其他页面链接到本页
  #6 stale_draft agent-overview [...]: draft 状态已持续 37 天未更新（阈值 14 天）

[issues.json] open 6（新增 6），本轮 auto_resolved 0，历史共 6 条
```

紧接着原样重跑：`open 6（新增 0）`——id 稳定、不重复。修掉断链再跑：
`open 5，本轮 auto_resolved 1`，#1 的 status 变为 `auto_resolved` 且历史保留。

## 局限性

- 只能查「形式」问题：链接断没断、来源在不在。**内容对不对**（页面说的和代码
  是否一致）机械规则查不了——那需要重读源码做判断，是 s09/s12 Agent 的领域；
- `missing_backlink` 与 `orphan_page` 在小型 Wiki 里偏噪（页面少、链接自然稀疏），
  实际使用可按 severity 过滤；
- 行号范围检查读了被引用的源文件，大项目下 lint 不再是纯 O(Wiki) 的操作
  （有缓存，每个文件只读一次）。

## 流水线篇小结（s01–s06）

- s01 把 LLM 变成函数（结构化输出 + 校验重试 + 截断）；
- s02 让输出有形（页面 + 可验证的行号引用 + 敏感文件红线）；
- s03 让它成批且皮实（幂等 manifest、op 队列、失败隔离、断点续跑、map-reduce）；
- s04 把散页收拾成 Wiki（去重、链接、死链、索引，「候选代码筛 + 定夺 LLM」）；
- s05 让 Wiki 跟上现实（git 增量、删除的体面处理）；
- s06 给整条线装上质量仪表（确定性 lint + issue 生命周期）。

**至此，模型始终没有决定过任何一步流程走向。** 下一阶段开始，控制流交给模型。

## 下一阶段（s07）

Agent 篇开篇：最小 Agent Loop——只有 bash 一个工具的循环 + MaxIterations 护栏。
同一个模型、同一套 API，但「谁掌握控制流」彻底翻转。
对照 `engine.go` 的 `executeLoop → runReActIteration` 与参考项目 learn-claude-code v0/v1。
