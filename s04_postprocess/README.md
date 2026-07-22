# s04：后处理流水线

## 本阶段目标

s03 产出的是一堆**散页**；s04 用一条确定性步骤链把它们收拾成一个**Wiki**：

```
python code.py postprocess
  ├─ 0 discover   扫盘同步 pages.json（磁盘是事实，状态是缓存）
  ├─ 1 dedup      页面去重合并：候选由代码筛，定夺由一次 LLM 调用
  ├─ 2 crosslink  交叉链接：标题/别名文本匹配 → 内联链接 + 重建「相关页面」
  ├─ 3 deadlink   死链清理：指向已合并页面的链接改写到重定向目标；
  │               目标不存在的解除为纯文本
  └─ 4 index      重建 index.md：分组目录（代码）+ 导读（LLM，带缓存）
```

## 上一阶段的问题

- **重复主题**：s02 手工生成的 `internal-agent-engine` 和 s03 归并出的
  `internal-agent` 讲的是同一块代码，读者不知道该看哪个；
- **页面孤立**：没有任何互相链接，「相关页面」小节全是占位文本；
- **没有入口**：没有 index，读者不知道 Wiki 里有什么。

## 新增机制

**1. 候选由代码筛，定夺由 LLM——本阶段最重要的成本模式。**
去重如果全交给 LLM，就要对 O(n²) 的页面对逐一比较；全交给代码，又判不了
「这两页是不是同一主题」这种语义问题。正确分工：代码用便宜的信号
（来源文件重叠、slug/标题相似度 ≥ 0.7）把候选缩到几对，每对送**一次** LLM 调用定夺。
定夺结果记入 `dedup_checked`——判过「不合并」的对不再重复送审（幂等 + 省钱）。

**2. 合并留下别名与重定向。** 被合并页面的标题成为主页面的 **alias**（对照 WeKnora
页面的 `Aliases` 字段），旧 slug 记入 **redirects**。别名参与后续交叉链接匹配；
重定向让死链清理能把旧链接**改写**到新位置而不是粗暴删除——真实 Wiki 合并词条
的标准做法。

**3. 交叉链接是纯文本匹配，不是 LLM。** 词表 = 全部页面标题 + 别名（≥4 字符，
长词优先）；每页正文里其他页面词条的**首次出现**转为相对链接。跳过规则对照
`linkifyContent`：标题行、代码围栏、行内代码、已链接文本都不动；页面里已有指向
目标的链接就不再注入（幂等）。「相关页面」小节每次**整节重建**（共享来源文件 /
共享来源目录 / 正文提及三类关系，标注原因），重建即幂等。

**4. 死链清理分两档。** 链接目标不存在时：目标 slug 在 redirects 里 → 改写到
重定向终点；否则 → 解除链接保留文字。链接图同时落入 pages.json（s06 lint 的输入）。

**5. index 导读带缓存。** 目录分组和每页一句话描述（取「概述」首句）是纯代码；
只有顶部导读是 LLM 生成，且按页面集合哈希缓存——页面没变就不再调用。

## 核心设计：为什么步骤顺序是 dedup → crosslink → deadlink → index

dedup 会删页面，所以链接注入必须在它之后（否则刚注入的链接立刻变死链）；
deadlink 在 crosslink 之后，处理的是**跨次运行**留下的旧链接（上次运行注入的链接，
这次可能因合并而失效）；index 最后跑，拿到的才是最终的页面集合。
每一步都幂等，整条链重跑一遍应当接近无事发生——验证步骤会专门检查这一点。

## 代码解析

| 部分 | 说明 |
|---|---|
| `parse_page` / `write_page_text` / `section_replace` | frontmatter 解析、写回、整节替换（幂等的基础） |
| `step_discover` | 扫盘同步 pages.json；s02 的手工页面在这里被接管 |
| `find_dedup_candidates` | 代码筛候选：来源重叠 / slug / 标题相似度，`dedup_checked` 跳过已判对，每轮上限 5 对 |
| `DEDUP_PROMPT` + `validate_judge` | 定夺 schema：merge / reason / primary_slug / merged；merged 的 source_path 必须 ⊆ 双方来源并集（反幻觉） |
| `step_dedup` | 合并执行：写主页面、删副页面、登记 alias + redirect（含重定向链压平） |
| `inject_inline_links` | 词表长词优先；跳过标题/围栏/行内代码/已链接；每页每目标最多一条 |
| `step_crosslink` | 内联注入 + 「相关页面」整节重建 + 链接图落库 |
| `step_deadlink` | 重定向改写 / 解除链接两档处理 |
| `step_index` | 分组目录 + 概述首句描述 + 导读（页面集合哈希缓存） |
| `MockLLM` | dedup：来源重叠→机械合并（primary=来源多的一方），无重叠→不合并；导读：固定文本 |

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版 |
|---|---|
| `step_dedup`（候选代码筛 + LLM 定夺） | `deduplicateExtractedBatch`（`wiki_ingest.go:2086`）——在**提取批次内**去重（同批文档抽出的重复实体/概念），教学版把同样的思想用在页面级 |
| `inject_inline_links` | `linkifyContent`（`wiki_linkify.go`）：注入 `[[slug|匹配文本]]` wiki-link，同样跳过已有链接、代码段、图片；WeKnora 用 `[[...]]` 语法（前端渲染），教学版用标准 Markdown 相对链接（任何查看器可用） |
| 别名参与匹配 | 页面 `Aliases` 字段（`tools/wiki_write_page.go`）；prompt 里也要求模型把别名写成 `[[slug|display]]`（`prompts_wiki.go` 的 Wiki-link rule） |
| `step_deadlink` | `cleanDeadLinks`（`wiki_ingest.go:1484`）用页面存活清单校验出链（`repository/wiki_page.go:705`）；s05 还会对照 `sanitizeDeadSummaryLinks` |
| `step_index` | `rebuildIndexPage`（`wiki_ingest.go:1876`）：同样是「结构由代码、导读由模型」 |
| type → 目录固定映射 | `planBatchTaxonomy`（`wiki_ingest_taxonomy.go:32`）：WeKnora 用 LLM 做批级目录规划（页面归入哪个分类）；教学版退化为 page_type 决定目录的固定映射 |

**教学版砍掉了什么**：批内提取级去重（我们只有页面级）、LLM 目录规划、
`[[...]]` wiki-link 语法与前端渲染、引用分类 pass（`wiki_ingest_cite.go`）、
并发下的页面存活校验（教学版单进程，读盘即最新）。

## 运行方法

```bash
cd C:\Desktop\Project\llm-wiki-learning\s04_postprocess

# 前置：workspace 里应有 s03 的页面（含 s02 的重叠页面作为去重素材）。
# 如需更丰富的演示，可先多 ingest 一个目录：
python ..\s03_map_reduce\code.py ingest --mock --path internal/agent/tools --limit 4

# 运行后处理
python code.py postprocess --mock

# 幂等验证：紧接着原样重跑，应当接近无事发生
# （dedup 无新候选、注入 0 处、死链 0 条、导读走缓存）
python code.py postprocess --mock

# 查看产物
type ..\workspace\wiki\index.md
type ..\workspace\wiki\modules\internal-agent.md
```

## 示例输出（首次运行）

```
[discover] 磁盘页面 4 个，pages.json 已同步
[dedup] 候选 2 对（上限 5），逐对送 LLM 定夺
  [合并] internal-agent-engine → internal-agent：来源重叠（internal/agent/engine.go）...
  [保留] internal-agent | internal-agent-tools：来源无重叠，判定为相关但不相同的主题
[crosslink] 内联链接注入 N 处，全部页面「相关页面」小节已重建
  [重写] agent-overview: ../modules/internal-agent-engine.md → internal-agent.md（重定向）
  [解除] agent-overview: ../modules/ghost.md 目标不存在，还原为纯文本
[deadlink] 重写 1 条（指向已合并页面），解除 1 条（目标不存在）
[index] 重建完成：4 个页面，2 个分组
```

## 局限性

- 相似度阈值 0.7 是拍的：偏低会送太多候选浪费钱，偏高会漏掉重复——
  生产系统需要按语料调参或用 embedding（本课程不引入向量）；
- 交叉链接是精确文本匹配：改个说法（「引擎」vs「Engine」）就匹配不上，
  WeKnora 靠 prompt 让模型在生成时就写 `[[...]]`，两种路线各有取舍；
- 「相关页面」的三类关系是启发式，会漏语义相关但来源不相交的页面；
- 合并是不可逆操作：LLM 误判会丢页面（redirects 保留了线索，但内容需人工找回）。
  这正是 s06 lint / s12 fixer 存在的意义之一。

## 下一阶段（s05）

增量更新：`state` 记录 `last_ingest_commit`，用 `git diff --name-status` 找出
新增/修改/删除的文件，只对受影响文件重跑 map-reduce；删除的文件不粗暴删页面
（先查页面是否还有其他来源），失效页面标 `deprecated`。
对照防抖触发（`wikiIngestDelay`）、`task_pending_ops`、`sanitizeDeadSummaryLinks`。
