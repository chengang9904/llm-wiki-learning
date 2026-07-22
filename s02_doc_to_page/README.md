# s02：单文件 → Wiki 页面（map 步骤）

## 本阶段目标

把 s01 的「结构化提取」升级为流水线的 **map 步骤**：

```
scan_sources()  ──▶ 候选文件清单（排除清单在这里系统性生效）
map <某个文件> ──▶ 页面级提取（带行号引用）──▶ 渲染 frontmatter 页面
                                              ──▶ workspace/wiki/<type>/<slug>.md
                                              ──▶ workspace/logs/wiki-log.md 追加记录
```

产出的页面结构（固定六节 + 未确认事项）：

```markdown
---
title: Agent ReAct 引擎
type: module            # architecture | module | workflow | api | data | infrastructure | decision | glossary
status: draft           # draft | verified | incomplete | deprecated
sources:
  - {path: internal/agent/engine.go, lines: 349-433}
updated_at: 2026-07-22
---
## 概述 / 核心职责 / 关键实现 / 调用关系 / 相关页面 / 证据与来源 / 未确认事项
```

## 上一阶段的问题

s01 的提取结果只打印到终端：没有落盘、没有固定结构、没有来源引用。
`{summary, key_symbols}` 这种扁平摘要也撑不起一个 Wiki——读者需要知道
「这个事实是从哪几行代码得出的」，否则 Wiki 和模型幻觉无法区分。
另外 s01 一次只看一个用户指定的文件，「哪些文件值得进 Wiki」这个问题还没人回答。

## 新增机制

**1. 扫描器（未来 manifest 的雏形）。** `os.walk` + 三层过滤：

- 目录剪枝：`.git`、`node_modules`、`frontend`、`web`、`.local-data`、`__pycache__`、
  `.venv`、`vendor` 等——在 `dirnames` 里原地删除，walk 根本不进入；
- 敏感文件红线（延续 s01）：`.env`、`*.pem` 等，**统计但绝不读取**；
- 噪音过滤：扩展名白名单（`.go/.py/.md/.yaml/.proto`）+ 测试/生成码排除
  （`*_test.go`、`*.pb.go`）。

**2. 可验证的行号引用。** 送给 LLM 的内容每行都带行号前缀，schema 要求
「关键实现」每条带 `lines: "起-止"`，校验器把行号和**真实文件行数**比对——
引用第 9000 行而文件只有 700 行，就是幻觉证据，打回重试。
这是「重要事实必须有来源」从口号变成机制的关键一步。

**3. 页面渲染与修改日志。** 提取结果 → frontmatter + 固定小节的 Markdown；
`slugify` 从文件路径确定性地导出页面路径（同一文件永远生成同一页面——s03 幂等的前提）；
每次写页面在 `workspace/logs/wiki-log.md` 追加：时间、页面、来源、原因、未确认事项数。

**4. 空内容守卫。** 实质字符少于 50 就拒绝调 LLM——没有内容还硬提取，模型只会编。

## 核心设计：为什么「不确定」是一个正式字段

schema 里 `uncertainties` 和 `summary` 平级。prompt 明确要求：没把握的推测
**不写进正文，写进 uncertainties**。这解决 LLM 摘要最大的问题——它从不说「我不知道」。
给不确定性一个合法出口，正文的可信度才有保障；这些未确认事项后续正是
s09 问答 Agent 和 s12 Fixer 的工作清单。`status: draft` 同理：页面生成即 draft，
`verified` 要等核实——生成和核实是两个环节。

## 代码解析（相对 s01 的增量，直接 `diff ../s01_llm_as_function/code.py code.py`）

| 新增/变化 | 说明 |
|---|---|
| `EXCLUDED_DIRS` / `EXCLUDED_FILE_PATTERNS` / `INCLUDE_EXTS` | 三层过滤清单 |
| `scan_sources` | os.walk + dirnames 原地剪枝，返回（候选，排除统计） |
| `number_lines` | 行号前缀，使引用可验证 |
| `EXTRACT_PAGE_PROMPT` | schema 升级为页面级 7 字段；新增「引用纪律」小节 |
| `validate_page_extraction(data, total_lines)` | 新增 page_type 枚举校验、lines 格式与**范围**校验 |
| `map_one_file` | 教学版 mapOneDocument：守卫 → 读取 → 行号 → 截断 → 提取重试 |
| `slugify` / `render_page` / `write_page` | 确定性页面路径、frontmatter 渲染、追加日志 |
| `MIN_TEXT_CHARS` 守卫 | 对照 hasSufficientTextContent |
| CLI 变为子命令 | `scan` / `map <file> [--mock]` |

s01 原样保留的部分：`load_env`、`extract_json_text`、重试循环骨架、截断上限、
`OpenAILLM`。MockLLM 的剧本换了：第 1 次故意 `page_type: "component"`（不在枚举）
+ 一条 `lines: "9000-9100"`（超出文件范围），专门演示 s02 新增的两条校验规则。

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版 |
|---|---|
| `map_one_file` | `mapOneDocument`（`wiki_ingest_batch.go:1109`）：从 chunk 库重建内容 → rune 截断 → 守卫 → LLM 提取。它的输入是已入库的文档 chunk，教学版直接读源文件 |
| 单次提取调用 | `extractEntitiesAndConceptsNoUpsert`（`wiki_ingest_batch.go:1554`）——名字里的 NoUpsert 就是「只提取不落库」，落库由 reduce 统一做（s03 同样安排） |
| `MIN_TEXT_CHARS` 空内容守卫 | `hasSufficientTextContent`（`wiki_ingest.go:2750`）：剥掉图片 markup 后无实质文本则跳过，防止对纯扫描件幻觉提取 |
| frontmatter 的 type/status/sources | `types/wiki_page.go`：`PageType`（summary/entity/concept/index/log/synthesis/comparison）、`Status`（draft/published/archived）、`SourceRefs`。教学版的 8 类 type 是课程自定分类，机制相同：type 决定页面归属与索引分组 |
| 行号引用 + 范围校验 | WeKnora 的引用单位是 **chunk 而非行号**（`wiki_ingest_cite.go` 的引用分类 pass），且 sourceRef 刻意只用 knowledge ID 不用文件名，防文件名泄漏进 prompt（`wiki_ingest_batch.go:1187` 注释）。教学版直接读文件，行号是更直观的引用单位 |
| `write_page` 直接写文件 | 生产版页面存数据库，写入发生在 reduce 阶段且带 per-slug 锁 |

**教学版砍掉了什么**：chunk 重建与 chunk 级引用、pass 0 候选 slug 提取 + legacy
双提取器回退、ingest/delete 竞态守卫（`isKnowledgeGone`——文档在任务排队期间被删除时
不能继续提取，否则产生指向幽灵文档的引用）、Langfuse span 追踪、多语言。
其中「竞态守卫」在单进程教学版里天然不存在，这正是 WeKnora 需要并发治理的原因之一。

## 运行方法

```bash
cd C:\Desktop\Project\llm-wiki-learning\s02_doc_to_page

# 1. 扫描：看排除清单如何生效（不调 LLM，无需 Key）
python code.py scan

# 2. map 一个文件（Mock，演示新校验规则的失败 → 重试）
python code.py map C:\Desktop\Project\WeKnora\internal\agent\engine.go --mock

# 3. 查看产出
type ..\workspace\wiki\modules\internal-agent-engine.md
type ..\workspace\logs\wiki-log.md

# 4. 验证边界：敏感文件、SOURCE_ROOT 之外的文件，都应被拒绝
python code.py map C:\Desktop\Project\WeKnora\.env --mock
python code.py map C:\Windows\win.ini --mock

# 真实 LLM：配置 .env 后去掉 --mock
```

## 示例输出（map --mock）

```
[调用] 第 1 次 LLM 调用 ...
[失败] 第 1 次尝试未通过：字段 "page_type" 必须是以下之一：architecture, module, ...;
       key_implementation[2].lines=9000-9100 超出文件真实行号范围 1-703（引用必须可验证）
[调用] 第 2 次 LLM 调用 ...
[通过] schema 校验通过（第 2 次尝试）

[写入] ...\workspace\wiki\modules\internal-agent-engine.md
[日志] ...\workspace\logs\wiki-log.md
```

## 局限性

- 还是一次一个文件、手工指定——scan 产出的清单没有被 map 消费；
- 没有状态：重跑 map 就是完整重做（也重复付费），没有「已处理」标记；
- 一个源文件 = 一个页面，1:1 映射。真正的 Wiki 需要把多个文件的证据**归并**到
  同一主题页面（比如「Agent 引擎」页面应当综合 engine.go + think.go + act.go）；
- 失败仍然直接退出进程。

## 下一阶段（s03）

批量 map-reduce 与幂等状态：manifest 记录哈希与已处理标记（重跑自动跳过）、
提取结果按主题 slug **归并**到同一页面（读旧页面 + 新证据 → LLM 归并调用）、
单文件失败进 pending.json 不中断整批、崩溃后从断点续跑。
对照 `ProcessWikiIngest` / `claimPendingList` / `reduceSlugUpdates` / `withSlugLock`。
