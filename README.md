# llm-wiki-learning：用双范式构建 LLM Wiki

一个精简但骨架完整的教学项目：扫描真实企业项目 **WeKnora**（`C:\Desktop\Project\WeKnora`，
一个 Go + Python + Vue 的 LLM 知识库框架），生成一套带来源引用、互相链接的 Markdown Wiki，
并用 Agent 基于 Wiki + 源码回答问题、修复页面。

本项目最重要的教学目标只有一个：**理解并亲手实现「双范式」——什么工作交给确定性流水线，
什么工作交给 ReAct Agent，以及为什么。**

## 双范式：模型即函数 vs 模型即决策者

| | 流水线（s01–s06） | Agent（s07–s12） |
|---|---|---|
| 控制流归谁 | **代码**。LLM 只在固定节点被模板化调用 | **模型**。读哪个文件、搜什么、何时停，模型决定 |
| LLM 的角色 | 一个返回结构化 JSON 的「函数」 | 一个持续对话、发起工具调用的「决策者」 |
| 适合的工作 | 批量、可重复、结构可预知：生成、归并、去重、链接、索引、Lint | 开放式、需要判断和探索：问答、调用链追踪、页面修复 |
| 关键性质 | 幂等可重跑、状态外置、成本可控、失败隔离 | 简单稳定的循环、原子工具、权限边界、上下文管理 |

判断一个能力该放哪一侧，问一个问题：**这件事的步骤能不能事先写死？**
能写死（对每个文件做同样的提取）→ 流水线；不能写死（回答一个没见过的架构问题）→ Agent。
用 Agent 做流水线的活是浪费 token 且不可复现；用流水线做 Agent 的活是用 if-else 穷举不了判断分支。

## WeKnora 的原型印证

这不是我们发明的划分——WeKnora 的 Wiki 功能本身就是这样实现的（以下文件均已核实存在）：

**Wiki 生成 = 确定性流水线**（`internal/application/service/wiki_ingest*.go`）。
asynq 任务驱动的 map-reduce：`claimPendingList` 认领待处理文档 → `mapOneDocument`
对每篇文档发起模板化 LLM 调用（内容截断至 `maxContentForWiki = 32768` 字符）→
`reduceSlugUpdates` 带 `withSlugLock` per-slug 锁归并页面 → `deduplicateExtractedBatch`
去重 → `planBatchTaxonomy` 目录规划 → `injectCrossLinks` 交叉链接 → `rebuildIndexPage`
重建索引 → finalize 发布。模型在这里是被流水线调用的「函数」，不决定流程走向。
`internal/agent/prompts_wiki.go` 只是 prompt 模板库。

**Wiki 问答与修复 = ReAct Agent**（`internal/agent/engine.go` 的
`executeLoop → runReActIteration`）。`wiki_write_page` / `wiki_flag_issue` 等工具
（`internal/agent/tools/`）只注册给交互式 Agent（`wiki-qa` agent 类型、内置
`builtin-wiki-fixer` 修复 Agent），不参与初始生成。另有确定性 Lint（`wiki_lint.go`）。

WeKnora 这样划分的原因：批量生成追求**成本可控、幂等重试、崩溃恢复、并发治理**，
确定性流水线更合适；问答和修复是**开放式任务**，需要模型自主探索，Agent Loop 更合适。
本项目复刻这个划分，但把生产级并发工程（slug 锁、在途配额、防抖、429 退避）
简化为单进程顺序执行——每个阶段的 README 都会说明砍掉了什么。

## 与 RAG 的区别

WeKnora 同时有 RAG 问答（chat_pipeline：查询 → 向量检索 → 重排 → 生成）和 Wiki 模式。
两者解决的问题不同：

- **RAG** 在**查询时**临时检索原始切片，回答一次性问题，不沉淀结构；
- **Wiki** 在**摄取时**把文档蒸馏成一套人可读、互相链接、带来源的知识页面，
  查询时 Agent 先读 Wiki 再按需下钻源码。

本项目只做 Wiki 这一边。**Embedding / 向量检索不在本项目范围内**——Wiki 导航靠
文件名、标题匹配和 grep，这足以支撑教学且不引入向量库依赖。

## 阶段总览

每个阶段是一个自包含目录：`README.md` + `code.py` + `.env.example` + 运行示例 + 验证步骤。
相邻阶段允许大量重复——直接 `diff` 两个阶段的 `code.py` 就能看到增量。

### 流水线篇：模型即函数

| 阶段 | 内容 | 对照 WeKnora |
|---|---|---|
| **s01** 结构化 LLM 调用 | 无循环无工具。一次调用返回结构化 JSON，schema 校验失败带错误重试，内容超限截断 | `generateWithTemplate`、`json_repair.go`、`maxContentForWiki` |
| **s02** 单文件 → Wiki 页面 | 扫描器（敏感文件排除清单）+ 提取 → 带 frontmatter 的页面，重要事实必须有来源 | `mapOneDocument` |
| **s03** 批量 map-reduce 与幂等状态 | manifest 哈希跳过已处理、按 slug 归并页面（读-改-写）、失败进 pending 不中断整批、崩溃续跑 | `ProcessWikiIngest`、`claimPendingList`、`reduceSlugUpdates`、`withSlugLock` |
| **s04** 后处理流水线 | 去重合并 → 交叉链接注入 → 清理死链 → 重建 index | `deduplicateExtractedBatch`、`injectCrossLinks`、`rebuildIndexPage` |
| **s05** 增量更新 | `git diff` 找变更文件，只重跑受影响部分；删除文件不粗暴删页面，先查其他来源 | 防抖触发、`task_pending_ops`、`sanitizeDeadSummaryLinks` |
| **s06** Wiki Lint | 纯确定性检查（不调 LLM）：断链、缺来源、孤立页面……输出结构化报告 | `wiki_lint.go`、`wiki_flag_issue` 的 issue 结构 |

### Agent 篇：模型即决策者

| 阶段 | 内容 | 对照 WeKnora |
|---|---|---|
| **s07** 最小 Agent Loop | 只有 bash 一个工具的循环 + MaxIterations 护栏 | `engine.go` 的 `executeLoop → runReActIteration` |
| **s08** 原子文件工具与权限边界 | list_files / read_file / grep / glob + 只读源码、敏感文件拒读、路径防穿越 | `tools/registry.go`、`definitions.go` |
| **s09** Wiki 问答 Agent | wiki 导航工具；回答优先级 Wiki → 引用源码 → 额外搜索；答案带引用 | `wiki-qa` agent 类型、`wiki_read_source_doc.go` |
| **s10** Todo 与子 Agent | todo 工具 + spawn_subagent（独立上下文），跨模块调用链追踪 | `tools/todo_write.go`；参考项目 v3 |
| **s11** 上下文工程 | Skill 按需加载 + 工具返回截断 + 接近上限时压缩历史 | `skills/`、`skill_read.go`、`truncate.go`、`agent/memory/` |
| **s12** Fixer 与完整整合 | Agent 消费 Lint 报告逐条修复；整合六命令 | `builtin-wiki-fixer`、`wiki_flag_issue/update_issue` |

Agent 篇的另一个参照物是 `C:\Desktop\Project\learn-claude-code`（v0–v4 五个自包含
单文件，Anthropic SDK）——借鉴其结构与思想，不复制代码。流水线篇的参照物是
WeKnora 的 `wiki_ingest` 本身。

## 安装与模型配置

```bash
cd C:\Desktop\Project\llm-wiki-learning
pip install -r requirements.txt
copy .env.example .env    # 填入你的配置
```

`.env` 三个变量（OpenAI 兼容 API，任何兼容网关/本地推理服务均可）：

```
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL_ID=gpt-4o-mini
```

**与参考项目（Anthropic SDK）的差异**，读 learn-claude-code 时注意换算：

| | Anthropic SDK | OpenAI 兼容（本项目） |
|---|---|---|
| system prompt | 独立的 `system=` 参数 | `messages` 里第一条 `{"role": "system"}` |
| 模型发起工具调用 | 响应 content 里的 `tool_use` 块 | `message.tool_calls` 列表（含 `function.arguments` JSON 字符串） |
| 返回工具结果 | user 消息里的 `tool_result` 块 | 独立的 `{"role": "tool", "tool_call_id": ...}` 消息 |
| 工具 schema | `input_schema` | `function.parameters` |

## 六命令（s12 最终形态）

```bash
python code.py init      # 初始化 workspace/
python code.py ingest    # 流水线：全量生成 Wiki
python code.py update    # 流水线：git diff 增量更新
python code.py lint      # 流水线：质量检查 → state/issues.json
python code.py query "问题"   # Agent：基于 Wiki + 源码回答
python code.py fix       # Agent：消费 issues.json 修复页面
python code.py status    # 查看 state
```

`ingest / update / lint` 走流水线，`query / fix` 走 Agent——命令行入口本身就是双范式的分界线。

## Wiki 数据目录

```
workspace/
├── raw/        # 源文件清单 manifest（路径、哈希、mtime）；只存清单，禁止复制源码快照
├── wiki/       # index.md, architecture/, modules/, workflows/, data/, api/,
│               # infrastructure/, decisions/, glossary/
├── state/
│   ├── sources.json    # 含 last_ingest_commit（git 增量更新用）
│   ├── pages.json      # 页面元数据与链接图
│   ├── pending.json    # 待处理/失败重试队列（对照 task_pending_ops）
│   ├── issues.json     # Lint/Agent 标记的问题（对照 wiki_flag_issue）
│   └── session.json    # Agent 工作状态
└── logs/wiki-log.md    # 每次修改：时间、页面、来源、原因、未确认事项
```

## 安全边界（自 s01 起生效）

WeKnora 根目录存在**真实 `.env`（含 API 密钥）**。因此：

1. 流水线的文件扫描器内置排除清单：`.env`、`.env.*`、`*.pem`、`*.key`、
   `credentials*`、`secret*` 等，连同 `.git/`、`node_modules/`、`.venv/` 等目录；
2. Agent 侧的 system prompt 和权限层同样禁止读取敏感文件；
3. 任何阶段都不得把敏感文件内容发送给 LLM；
4. 对 WeKnora 目录只读；写操作只允许发生在本项目内部。

## 已知局限

- 单进程顺序执行，没有 WeKnora 的并发治理（slug 锁在教学版里退化为顺序执行天然满足）；
- 无向量检索，Wiki 导航靠标题/文本匹配；
- schema 校验是手写规则而非 JSON Schema 库，覆盖教学所需即可；
- 教学优先：宁可重复代码，不做过度抽象。

## 当前进度

- [x] s01_llm_as_function
- [x] s02_doc_to_page
- [x] s03_map_reduce
- [x] s04_postprocess
- [x] s05_incremental_update
- [ ] s06–s12
