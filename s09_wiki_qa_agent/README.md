# s09：Wiki 问答 Agent

## 本阶段目标

让 Agent 用上 s01–s06 建的 Wiki。新增三个导航工具 + 一套回答纪律：

```
python code.py query "文档上传后经过哪些处理步骤？"

回答优先级（三级瀑布）：
  1. 先查 Wiki      search_wiki / list_wiki_pages → read_wiki_page
  2. 核实引用       read_file 打开页面引用的 路径:行号（Wiki 可能过时）
  3. 搜索兜底       grep / glob 补 Wiki 覆盖不到的部分

回答结构（五节）：结论 / 调用链 / 相关页面 / 源文件引用 / 不确定事项
```

## 上一阶段的问题

s08 的 Agent 每次回答都从零 grep：问十次「Agent 引擎怎么工作」就要重复十次
同样的探索。而流水线明明已经把这些知识蒸馏成了带引用的 Wiki 页面——
**两个范式各自建好了，但还没接上**。

## 新增机制

**1. 三个 Wiki 导航工具。**
- `list_wiki_pages`：按类型分组的全量清单（标题/状态/来源数/**别名**）；
- `read_wiki_page(slug)`：整页阅读；**自动跟随 s04 合并留下的重定向**——
  模型拿着旧 slug 也能到达正确页面，并被告知发生了重定向；
- `search_wiki(query)`：标题/别名/正文的文本搜索（无向量，课程边界）。
  别名参与搜索意味着 s04 合并时保留的「曾用名」在问答时兑现价值。

**2. 三级瀑布是 prompt 纪律，不是代码逻辑。** 注意实现里**没有**任何
「先调 search_wiki 再允许 grep」的硬编码——顺序完全由 system prompt 约定，
模型自主执行。这是 Agent 范式的正确姿势：把策略写进提示词，把能力做成工具，
让模型自己编排。（反例：在循环里写 if 强制顺序——那是往 Agent 里掺流水线，
两边的坏处都占。）

为什么是这个顺序：Wiki 是预蒸馏知识，一页顶几十次 grep 且自带引用；
但它可能过时（上次 update 之后代码又改了），所以**关键事实必须回源码核实**；
源码永远是最终事实。第 2 级正是 s02 埋下的伏笔——页面引用带行号，
就是为了此刻能被一条 read_file 精确核实。

**3. 不一致是产出，不是尴尬。** 五节结构里「不确定事项」明确要求：
发现 Wiki 与源码不一致就记录在此。问答 Agent 因此兼任 Wiki 的**巡检员**——
这些发现流向 issues.json（s12 的 Fixer 消费），与 s06 lint 的机械检查互补：
lint 查形式（链接断没断），问答顺手查**内容**（说的对不对）。

## 代码解析（相对 s08 的增量）

| 新增/变化 | 说明 |
|---|---|
| `tool_list_wiki_pages` | 磁盘为准 + pages.json 补元数据（状态/别名） |
| `tool_read_wiki_page` | slug 定位 + redirects 跟随 + index 特例 |
| `tool_search_wiki` | 标题/别名/正文三路命中，每页最多 3 条摘录，全局 25 条上限 |
| `SYSTEM_PROMPT` | 三级瀑布 + 五节结构 + 「不一致记入不确定事项」 |
| 其余 | 权限层、源码工具、注册表、Loop 与 s08 逐字相同 |

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版 |
|---|---|
| `query` 命令的 Agent | `wiki-qa` agent 类型（`config/agent_type_presets.yaml:68`，`types/custom_agent.go:51` 的 `AgentTypeWikiQA`）：预设同样的 Wiki 优先工具集与提示词 |
| `read_wiki_page` / `search_wiki` | `wiki_read_page` / `wiki_search`（`tools/definitions.go:25,30`） |
| 第 2 级「read_file 核实引用」 | `wiki_read_source_doc.go`：用知识点 ID 精读原始文档——生产版引用单位是 chunk/知识点（上传的文档不在文件系统里），教学版是 文件:行号 |
| 三级瀑布写在 system prompt | 同样写在 agent 预设的提示词里（`prompts.go` 家族），不是引擎代码 |
| 流水线侧的呼应 | `chat_pipeline/wiki_boost.go`：RAG 检索命中 Wiki 页面时**加权**——同一个「蒸馏知识优先于原始切片」原则，在流水线范式里的实现。双范式各有一个 Wiki 优先，这是两条路径互补的最好例证 |

**教学版砍掉了什么**：向量检索（`wiki_search` 生产版可走语义检索，教学版纯文本
匹配）、chunk 级引用与 `[[slug]]` wiki-link 渲染、会话持久化与流式输出、
问答结果回灌 RAG 的混合模式（hybrid-rag-wiki agent 类型）。

## 运行方法

```bash
cd C:\Desktop\Project\llm-wiki-learning\s09_wiki_qa_agent

# 真实 LLM（Wiki 覆盖越全效果越好；当前教学 workspace 只 ingest 了少数目录）
python code.py query "文档上传后经过哪些处理步骤？"

# Mock：脚本化三级瀑布，工具真实执行
python code.py query "Agent 引擎的循环是怎么工作的？相关代码在哪？" --mock
```

## 示例输出（--mock，节选）

```
[工具] search_wiki({"query": "Agent ReAct 引擎"})
[输出] agent-overview — …（命中: 正文）
       internal-agent — 模块 internal-agent（命中: 别名）      ← s04 的别名兑现

[工具] read_wiki_page({"slug": "internal-agent-engine"})
[输出] [重定向] internal-agent-engine 已被合并进 internal-agent，以下是 …  ← 重定向跟随

[工具] read_file({"path": "internal/agent/engine.go", "offset": 384, "limit": 12})
[输出] 386| 	for state.CurrentRound < e.config.MaxIterations {   ← 第 2 级核实

[工具] grep({"pattern": "func .*handleMaxIterations", ...})
[输出] finalize.go:160: func (e *AgentEngine) handleMaxIterations(   ← 第 3 级兜底
```

最终回答按五节输出，源文件引用与真实核实结果一致。

## 局限性

- 文本搜索召回有限：换个说法就搜不到（「上传流程」vs「ingestion」）——
  生产版用向量检索解这个问题，课程刻意不引入；
- 教学 workspace 的 Wiki 只覆盖了少数目录，多数问题会落到第 3 级搜索兜底——
  对 WeKnora 全量跑一次 s03 ingest（配真实 LLM）后问答质量会显著不同；
- 复杂问题（跨模块调用链）在单一上下文里展开会迅速膨胀——s10 的子 Agent 解决；
- 发现的不一致目前只写进回答文本，还没有落到 issues.json 的工具——s12 补上
  （对照 wiki_flag_issue）。

## 下一阶段（s10）

Todo 与子 Agent：todo_write/read/update（计划由模型生成不写死）+
spawn_subagent（独立上下文、不继承完整历史、返回结构化结果
{summary, important_files, call_chains, uncertainties}）。
验证任务：跨模块调用链追踪（如「上传文档到向量入库的完整链路」）。
对照 `tools/todo_write.go`；子 Agent 对照 learn-claude-code v3。
