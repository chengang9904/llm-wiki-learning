# llm-wiki-learning 开发提示词（原始规格，权威版本）

> 本文件是本项目的原始开发提示词，逐字保存，作为 s01–s12 全程的权威规格。
> 上下文压缩后，从这里恢复完整要求。执行进度见文末「进度追踪」。
> 注：个别行在原始输入中即被截断（如「prompts_wiki.go 只是 prompt 模板库」前的
> 片段），已按第一轮源码核实结果在括号内补注。

---

你是一名资深 Agent Harness 工程师、软件架构师和代码教学专家。

## 〇、背景事实（均已对照源码核实，勿凭空更改）

- **待分析的企业项目**：`C:\Desktop\Project\WeKnora`。LLM 知识库框架（RAG 问答 +
  ReAct Agent + Wiki 模式）。技术栈：Go 后端（`cmd/server`、`internal/`，uber/dig，
  Gin + GORM，asynq），Python docreader gRPC 文档解析服务（`docreader/`），
  Vue 3 前端（`frontend/`），独立 Go CLI（`cli/`）。
  分层：`internal/handler → internal/application/service → internal/application/repository`。
  这不是 Java/Spring 项目。项目详情见其根目录 `CLAUDE.md`。

- **WeKnora 的 Wiki 是双范式实现，学习项目将忠实模仿这一架构**：
  1. **Wiki 生成 = 确定性流水线**（`internal/application/service/wiki_ingest*.go`）。
     asynq 任务驱动的 map-reduce：认领待处理文档（`claimPendingList`）→
     `mapOneDocument` 对每篇文档发起模板化的单次 LLM 调用（ext summary，
     内容截断至 32KB，`maxContentForWiki = 32768`）→ `reduceSlugUpdates`
     带 per-slug 锁归并页面 → 去重（`deduplicateExtractedBatch`）→
     目录规划（`planBatchTaxonomy`，wiki_ingest_taxonomy.go）→
     引用分类（`wiki_ingest_cite.go`）→ 交叉链接（`injectCrossLinks`）→
     重建索引（`rebuildIndexPage`）→ finalize 发布。
     模型在这里是被流水线调用的「函数」，不决定流程走向。
     `internal/agent/prompts_wiki.go` 只是 prompt 模板库。
  2. **Wiki 问答与修复 = ReAct Agent**（`internal/agent/engine.go` 的
     `executeLoop → runReActIteration`）。`wiki_write_page` /
     `wiki_flag_issue` 等工具只注册给交互式 Agent（`wiki-qa` agent 类型、
     内置 `builtin-wiki-fixer` 修复 Agent），用于问答导航和页面修复，
     不参与初始生成。另有确定性 Lint（`wiki_lint.go`）。

  WeKnora 这样划分的原因（README 必须讲清楚）：批量生成追求**成本可控、幂等重试、
  崩溃恢复、并发治理**，确定性流水线更合适；问答和修复是**开放式任务**，
  需要模型自主探索，Agent Loop 更合适。学习项目复刻这个划分，但把生产级并发
  工程（slug 锁、在途配额、防抖、429 退避）简化为单进程顺序执行。

- **教学参考项目**：`C:\Desktop\Project\learn-claude-code`。
  v0–v4 五个自包含单文件（bash agent → 基础工具 → todo → 子 Agent → skills），
  Anthropic SDK。它只覆盖本课程的 Agent 半边（s07–s12）；流水线篇
  的参照物是 WeKnora 的 wiki_ingest 本身。借鉴结构与思想，不复制代码。

- **学习项目位置**：`C:\Desktop\Project\llm-wiki-learning`（与 WeKnora 同级）。

- **LLM 接口**：OpenAI 兼容 API（`openai` SDK），`.env` 配置
  `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL_ID`。根 README 说明
  tool-calling 消息结构与参考项目 Anthropic SDK 的差异。

- **安全红线（自 s01 起生效）**：WeKnora 根目录存在真实 `.env`（含 API 密钥）。
  流水线的文件扫描器必须内置排除清单（`.env`、`.env.*`、`*.pem`、`*.key` 等）；
  Agent 侧的 system prompt 和权限层同样禁止读取。任何阶段都不得把敏感文件
  内容发送给 LLM。

- **每个阶段的 README 必须包含「对照 WeKnora 真实实现」一节**，指明
  对应的 WeKnora 源文件（第五节已给出映射），并说明教学版相对生产版砍掉了什么。

## 一、项目目标

在 `C:\Desktop\Project\llm-wiki-learning` 创建一个精简但骨架完整的 LLM Wiki
学习项目，模仿 WeKnora 的双范式架构：

批量路径（确定性流水线，模型即函数）：
  源文件清单 → 逐文件结构化提取 → 归并为 Wiki 页面 → 去重/链接/索引
  → Lint 检查 → 增量更新（只重跑受变更影响的部分）

判断路径（ReAct Agent，模型即决策者）：
  用户问题 / Lint 发现的问题
  → Agent 自主阅读 Wiki 和源码 → 追踪调用链 → 带引用回答 / 修复页面

能力要求：
1. 扫描 WeKnora，经流水线生成带来源引用和互相链接的 Markdown Wiki；
2. 项目变化后只增量更新受影响页面；
3. Lint 检查 Wiki 质量并输出结构化报告；
4. Agent 基于 Wiki + 源码回答架构、模块、调用链问题；
5. Agent 消费 Lint 报告修复页面；
6. 清楚展示两种范式各自的实现方式和适用边界。

Embedding / 向量检索不是本项目内容。

## 二、设计原则

### 1. 各归其位：这是本项目最重要的原则

**批量、可重复、结构可预知的工作交给流水线**（生成、更新、去重、链接、Lint）：
控制流由代码决定，LLM 只在固定节点被模板化调用，输出结构化数据并校验。

**开放式、需要判断和探索的工作交给 Agent**（问答、调用链追踪、页面修复）：
控制流由模型决定——读哪个文件、搜什么符号、是否继续追踪、证据是否充分。

不要用 Agent 去做流水线擅长的事（浪费 token、不可复现），
也不要用流水线去做 Agent 擅长的事（if-else 无法穷举判断分支）。
每个阶段的 README 都要能回答：这个能力为什么放在这一侧？

### 2. 流水线侧原则
- **幂等可重跑**：同样输入重跑一遍，结果一致且不重复劳动（靠 state/ 中的
  哈希与已处理标记判断跳过）；
- **状态外置**：进度存 state/ 的 JSON 文件，进程崩溃后可从断点继续；
- **结构化输出**：LLM 返回 JSON，schema 校验失败则带错误信息重试；
- **成本可控**：内容截断（对照 WeKnora 的 32KB 上限）、批大小限制、失败重试上限；
- **失败隔离**：单个文档失败不中断整批，记入失败清单待重跑。

### 3. Agent 侧原则
- **简单稳定的 Loop**：
  ```python
  while True:
      response = call_llm(system_prompt, messages, tools)
      messages.append(response)
      if response 不含 tool calls:
          return response.text
      messages.append(execute_tool_calls(response.tool_calls))
  ```
  后续只增加工具与状态，不重写循环；设最大迭代数护栏（对照 MaxIterations）。
- 模型决策、Harness 执行：Harness 提供工具、权限、上下文管理、护栏。
- 工具原子化：一个工具一个职责，新增工具 = schema + handler + 注册。

### 4. 每阶段独立可运行

每阶段目录：README.md + 自包含 code.py + .env.example + 运行示例 + 验证步骤。
相邻阶段允许大量重复——学习者应能 diff 两个阶段直接看到增量。

### 5. 教学优先

简单、明确、可运行；避免过度抽象；关键代码带中文注释。

## 三、总体目录结构

```
llm-wiki-learning/
├── s01_llm_as_function/      # 流水线篇
├── s02_doc_to_page/
├── s03_map_reduce/
├── s04_postprocess/
├── s05_incremental_update/
├── s06_wiki_lint/
├── s07_agent_loop/           # Agent 篇
├── s08_file_tools_permissions/
├── s09_wiki_qa_agent/
├── s10_todo_subagent/
├── s11_context_engineering/
├── s12_fixer_and_complete/
├── skills/  ├── examples/  ├── tests/
├── workspace/
├── .env.example  ├── requirements.txt  └── README.md
```

## 四、Wiki 数据目录

```
workspace/
├── raw/        # 源文件清单 manifest（路径、哈希、mtime、是否已处理）
│               # 只存清单，禁止复制源码快照
├── wiki/       # index.md, architecture/, modules/, data/, api/,
│               # workflows/, infrastructure/, decisions/, glossary/
├── state/
│   ├── sources.json    # 含 last_ingest_commit（git 增量更新用）
│   ├── pages.json      # 页面元数据与链接图
│   ├── pending.json    # 流水线待处理/失败重试队列（对照 task_pending_ops）
│   ├── issues.json     # Lint/Agent 标记的问题（对照 wiki_flag_issue）
│   └── session.json    # Agent 工作状态
└── logs/wiki-log.md    # 每次修改：时间、页面、来源、原因、未确认事项
```

## 五、渐进式实现阶段

### ═══ 流水线篇（s01–s06）：模型即函数 ═══

**s01：结构化 LLM 调用**

无循环、无工具。一次调用：给定一个源文件内容 + 模板 prompt，返回结构化 JSON
（{summary, key_symbols, responsibilities, references}），schema 校验失败
带错误重试（最多 2 次），内容超限截断。
README 解释：为什么「LLM 即函数」是流水线的基石；JSON 修复的常见坑。
验证任务：对 internal/agent/engine.go 提取结构化摘要。
对照 WeKnora：generateWithTemplate（wiki_ingest.go）、json_repair.go、
prompts_wiki.go、maxContentForWiki 截断。

**s02：单文件 → Wiki 页面（map 步骤）**

扫描器（含敏感文件排除清单与目录排除：.git、node_modules、frontend/、
web/、.local-data/、__pycache__、.venv 等）+ s01 的提取 → 渲染为带
frontmatter 的页面：
```
---
title: …
type: architecture | module | workflow | api | data | infrastructure | decision | glossary
status: draft | verified | incomplete | deprecated
sources: [{path: internal/…/xx.go, lines: 10-80}]
updated_at: YYYY-MM-DD
---
## 概述 / 核心职责 / 关键实现 / 调用关系 / 相关页面 / 证据与来源
```
重要事实必须有来源；不确定的进「未确认事项」。
对照 WeKnora：mapOneDocument、extractEntitiesAndConceptsNoUpsert。

**s03：批量 map-reduce 与幂等状态**

多文件批处理：manifest 记录哈希与已处理标记（重跑自动跳过）；
提取结果按主题 slug 归并到同一页面（reduce：读旧页面 + 新证据 → LLM 归并调用）；
单文件失败进 pending.json 不中断整批；python code.py ingest 可断点续跑。
README 讲：为什么 reduce 需要「读-改-写」；WeKnora 为什么给这一步上 slug 锁
（并发批次），而教学版单进程顺序执行不需要。
对照 WeKnora：ProcessWikiIngest、claimPendingList、reduceSlugUpdates、
withSlugLock、wikiMaxDocsPerBatch、requeueFailedOps。

**s04：后处理流水线**

确定性步骤链：页面去重合并（候选由代码筛，定夺由一次 LLM 调用）→
交叉链接注入（扫描全部页面标题/别名做文本匹配）→ 清理死链 →
重建 index.md（分组目录 + LLM 生成导读）。
对照 WeKnora：deduplicateExtractedBatch、planBatchTaxonomy、
injectCrossLinks、cleanDeadLinks、rebuildIndexPage、wiki_linking。

**s05：增量更新**

python code.py update：state 记录 last_ingest_commit，
git diff --name-status <commit>..HEAD 得到新增/修改/删除文件（哈希对比作
非 git 目录 fallback）→ 只对受影响文件重跑 map-reduce → 删除的文件不粗暴
删页面，先查页面是否仍有其他来源，失效页面标 deprecated → 写变更日志。
必须重新读源文件，不许只凭旧摘要更新。
对照 WeKnora：防抖触发（wikiIngestDelay）、task_pending_ops、
sanitizeDeadSummaryLinks；教学版用 git diff 替代其数据库触发机制。

**s06：Wiki Lint**

python code.py lint：纯确定性检查（不调 LLM）：断链、不存在的来源文件、
缺来源页面、长期 draft、标题冲突、孤立页面、缺反向链接、index 未收录。
输出结构化报告写入 state/issues.json，不自动修改。
对照 WeKnora：wiki_lint.go、wiki_flag_issue 的 issue 数据结构。

### ═══ Agent 篇（s07–s12）：模型即决策者 ═══

**s07：最小 Agent Loop**

只有 bash 一个工具的最小循环 + MaxIterations 护栏。system prompt 含安全红线。
验证任务：「WeKnora 用了哪些技术栈？各顶层目录职责是什么？」
README 解释：Loop 为什么简单；与 s01「LLM 即函数」的本质区别（谁掌握控制流）。
对照 WeKnora：engine.go 的 executeLoop → runReActIteration（说明生产版
额外包裹 token 估算、memory consolidator、Langfuse 追踪，教学版只留骨架）。
对照参考项目：learn-claude-code v0/v1。

**s08：原子文件工具与权限边界**

新增 list_files / read_file / grep / glob（统一注册表、行数限制、
目录排除）。权限层：SOURCE_ROOT=C:\Desktop\Project\WeKnora 只读，只允许写
llm-wiki-learning/ 内部，敏感文件连读都拒绝，路径规范化防穿越
（注意 Windows 路径），bash 命令黑名单 + 超时。
对照 WeKnora：tools/registry.go、definitions.go、scope_authorizer。

**s09：Wiki 问答 Agent**

新增 wiki 导航工具：list_wiki_pages / read_wiki_page / search_wiki。
python code.py query "文档上传后经过哪些处理步骤？"
回答优先级：Wiki → Wiki 引用的源码 → 额外源码搜索。
回答含：结论、调用链、相关页面、源文件引用、不确定事项。
对照 WeKnora：wiki-qa agent 类型、wiki_read_source_doc.go、
chat_pipeline/wiki_boost.go。

**s10：Todo 与子 Agent**

新增 todo_write/read/update（pending/in_progress/completed/blocked，计划由
模型生成不写死）+ spawn_subagent（独立上下文、不继承完整历史、
结果 {summary, important_files, call_chains, uncertainties}；主 Agent 综合）。
验证任务：跨模块调用链追踪（如「上传文档到向量入库的完整链路」）。
对照 WeKnora：tools/todo_write.go；子 Agent 对照 learn-claude-code v3。

**s11：上下文工程（Skill 按需加载 + 压缩）**

skills/（analyze-go-project.md、analyze-vue-project.md、
analyze-python-service.md、analyze-grpc-service.md、analyze-database.md、
trace-call-chain.md、verify-sources.md、fix-wiki-page.md），启动只载
名称+一行说明，需要时 load_skill。压缩：截断过大工具返回；接近上下文上限时
压缩历史（保留任务/关键发现/未完成事项），压缩后能继续不重扫。
对照 WeKnora：skills/、skill_read.go、truncate.go、sanitize_messages、
agent/memory/。

**s12：Fixer Agent 与完整整合**

Fixer：python code.py fix 读取 state/issues.json，Agent 逐条判断（重读
来源 → 修复 / 标 deprecated / 报告无法修复），修 Wiki 页面需带来源。
整合六命令：init / ingest / update / lint / query / fix / status——
ingest/update/lint 走流水线，query/fix 走 Agent。
根 README 收尾：双范式对比表（成本、可复现性、可恢复性、灵活性、适用场景），
以及 WeKnora 在此之上还做了哪些生产化工作（并发治理、多租户、审计等）。
对照 WeKnora：builtin-wiki-fixer（custom_agent.go）、wiki_flag_issue /
wiki_read_issue / wiki_update_issue、wiki_replace_text.go。

## 六、WeKnora 分析重点（Wiki 内容的目标）

重点识别：启动入口（cmd/server）、dig 容器装配（internal/container/container.go
是全系统地图）、Standard/Lite 双版本分支（按 REDIS_ADDR 切换 asynq 与同步
执行器）、配置加载、数据库迁移（Postgres 与 SQLite 两套）、知识库与文档
上传、docreader gRPC 解析、切分、Embedding、向量库注册表（RETRIEVE_DRIVER）、
chat_pipeline 插件流水线、Prompt 模板、LLM 调用与流式、会话管理、
RBAC、asynq 任务与 worker 池治理、Wiki 双范式本身、前后端关系、部署。

必须追踪的调用链（引用实际文件与函数）：
1. 创建知识库：handler → service → repository → DB；
2. 上传文档：API → 文件存储 → docreader gRPC → 切分 → Embedding → 向量存储
   → asynq 状态更新；
3. 问答：chat_pipeline 插件流水线（历史 → 查询理解 → 并行检索 → 合并 →
   top-k → 重排 → 流式输出）→ 会话持久化；
4. Wiki 摄取：wiki_ingest 的 map-reduce 全链路（这正好是学习项目 s03 的原型）。

## 七、首批 Wiki 页面（建议，由流水线按实际内容决定，不机械建空页）

index、architecture/{system-overview, directory-structure, backend-architecture,
frontend-architecture, lite-vs-standard}、modules/{knowledge-base,
document-management, docreader, chat-pipeline, agent, wiki-ingest, embedding,
vector-store, llm-client}、workflows/{document-ingestion, rag-query}、
data/database-model、api/api-overview、infrastructure/deployment、
glossary/domain-terms

## 八、测试要求

流水线侧：结构化输出校验与重试、map 幂等（重跑跳过）、reduce 归并、交叉
链接、git 变更检测、deprecated 判定、lint 规则。
Agent 侧：Loop 终止与迭代上限、工具注册执行、路径权限（含穿越攻击）、
子 Agent 隔离、skill 加载、压缩后可继续、fixer 判断分支。
全部提供 Mock LLM，无 API Key 也能跑基础测试。

## 九、README 要求

根 README：LLM Wiki 是什么；双范式架构及 WeKnora 的原型印证；与
RAG 的区别；阶段总览（流水线篇/Agent 篇）；安装与模型配置（OpenAI 兼容，
与参考项目 Anthropic SDK 的差异）；六命令用法；安全边界；已知局限。
每阶段 README：本阶段目标 / 上一阶段的问题 / 新增机制 / 核心设计 / 代码解析 /
对照 WeKnora 真实实现 / 运行方法 / 示例输出 / 局限性 / 下一阶段预告。

## 十、执行方式

现在不要一次性生成全部代码。首先只做：

1. 扫描 WeKnora（从 CLAUDE.md 与 internal/application/service/、
   internal/agent/ 入手），验证第〇节的双范式描述；
2. 输出简短的初步分析（技术栈、主要目录、Wiki 双范式的关键文件）；
3. 创建 C:\Desktop\Project\llm-wiki-learning\README.md，含 s01–s12 路线
   （流水线篇 / Agent 篇分册）；
4. 只实现 s01_llm_as_function（代码必须足够小，初学者能完整读懂）；
5. 运行验证 s01：对 internal/agent/engine.go 做一次结构化提取，覆盖
   schema 校验与重试路径（可用 Mock LLM 演示失败重试）；
6. 输出新增文件和运行结果；
7. 停止，不要继续实现 s02。

---

## 进度追踪（随开发更新）

- [x] **s01_llm_as_function** — 2026-07-22 完成并验证（Mock 重试路径、
  敏感文件拒绝、32KB 截断三条路径全部通过）。双范式描述已对照 WeKnora
  源码逐项核实（关键行号：claimPendingList wiki_ingest.go:667、
  mapOneDocument wiki_ingest_batch.go:1109、reduceSlugUpdates :1633、
  executeLoop engine.go:351、maxContentForWiki wiki_ingest.go:39）。
- [ ] s02_doc_to_page
- [ ] s03_map_reduce
- [ ] s04_postprocess
- [ ] s05_incremental_update
- [ ] s06_wiki_lint
- [ ] s07_agent_loop
- [ ] s08_file_tools_permissions
- [ ] s09_wiki_qa_agent
- [ ] s10_todo_subagent
- [ ] s11_context_engineering
- [ ] s12_fixer_and_complete
