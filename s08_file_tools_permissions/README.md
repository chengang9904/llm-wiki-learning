# s08：原子文件工具与权限边界

## 本阶段目标

把 s07 的「一个 bash 打天下」升级为**原子工具集 + 统一注册表 + 权限层**：

```
工具：bash（逃生舱）、list_files、read_file、grep、glob、write_file
注册：@tool 装饰器 → TOOL_REGISTRY → schema 自动汇入 API 调用
权限：resolve_path() 是所有文件访问的必经之路
       读 = SOURCE_ROOT（只读）∪ llm-wiki-learning
       写 = 只有 llm-wiki-learning 内部
       敏感文件连读都拒；路径规范化后再检查（防 .. 穿越）
```

Agent Loop 与 s07 **完全相同，一个字没改**——s07 的承诺（能力长在工具里，
循环不动）在本阶段第一次兑现。

## 上一阶段的问题

- bash 太粗：模型要自己拼 `grep -rn`，输出格式不稳、大小无界；
- 红线靠命令文本匹配，`cat .e*v`、`cat $(echo .env)` 都能绕过；
- 没有写能力，未来的 Wiki 修复（s12）无从谈起——但放开写就必须先有边界。

## 新增机制

**1. 工具注册表。** `@tool(name, description, parameters)` 装饰器把 schema 和
handler 一起登记进 `TOOL_REGISTRY`；循环启动时 `[entry["schema"] for ...]` 汇成
API 的 tools 参数，执行时按名分发。**新增工具 = 写一个带装饰器的函数**，
循环与分发代码零改动——这正是「一个工具一个职责，新增工具 = schema + handler + 注册」
设计原则的落地。`execute_tool` 统一兜错：权限拒绝、参数错误、工具内部异常
都变成返回给模型的文字，**任何工具的任何失败都不能炸掉循环**。

**2. 原子工具的共同纪律。** 每个工具自带输出上限（grep 50 条、glob 100 条、
list 200 条、read_file 单次 500 行）+ 全局 4000 字符截断——上下文是 Agent 最贵的
资源，工具层先守第一道门（s11 систематизирует）。read_file 输出带**行号**，
和 s02 以来的引用格式（`路径:行号`）一脉相承：Agent 读到的每一行都自带引用坐标。

**3. 权限层：位置比规则重要。** s07 的教训是「在命令文本上做检查」防不住绕过；
s08 把检查移到**文件访问的必经之路**——`resolve_path()`：
- 先 `Path.resolve()` 规范化（展开 `..`、符号链接），**再**做一切检查——
  `internal/agent/../../.env` 解析成真实 `.env` 后逃不过敏感清单；
- Windows 细节：路径大小写不敏感，比较用 `casefold()`；加尾分隔符防
  `C:\foo` 前缀误匹配 `C:\foobar`；
- 读白名单两个根、写白名单一个根；敏感文件在两种模式下都拒；
- list/grep/glob 连敏感文件的**名字都不展示**（存在性也是信息）。

bash 仍是文本匹配红线——它是显式保留的「粗粒度逃生舱」，README 直说这是
教学取舍：生产系统要么给 bash 也套沙箱（容器/受限 shell），要么干脆不给 bash。

**4. write_file 与边界演示。** 放开写能力的同一个提交里就有写边界：
写 SOURCE_ROOT 被拒（「一个字节都不许改」），写 workspace/ 放行。
验证脚本三连越权全部被拒后，向 workspace 写笔记成功——权限层不是禁止行动，
是给行动划清地盘。

## 代码解析

| 部分 | 说明 |
|---|---|
| `TOOL_REGISTRY` + `@tool` | 注册表；schema 与 handler 同处一地 |
| `execute_tool` | 按名分发 + 统一兜错 + 全局截断 |
| `resolve_path` / `_is_within` | 权限层核心：规范化 → 敏感清单 → 根白名单 |
| `tool_read_file` | 行号输出 + offset/limit 分段 + 500 行上限 |
| `tool_grep` / `tool_glob` / `tool_list_files` | 目录排除、敏感文件隐身、结果上限 |
| `tool_write_file` | 只写 PROJECT_ROOT；相对路径落 PROJECT_ROOT（读落 SOURCE_ROOT） |
| `agent_loop` | 与 s07 逐字相同（分发改查注册表） |
| `MockLLM` | 6 轮脚本：glob→grep→read_file 真实定位 executeLoop；三连越权（根外读/穿越读 .env/写 SOURCE_ROOT）；合法写入；收敛 |

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版 |
|---|---|
| `TOOL_REGISTRY` + `@tool` | `tools/registry.go`：`ToolRegistry.RegisterTool`（:47），Agent 构建时按 agent 类型/能力挑选注册哪些工具（`capabilities.go` 的 KBCapability 门控——wiki 工具只给有 CapWiki 的 KB） |
| 工具名集中定义 | `tools/definitions.go`：`ToolKnowledgeSearch` / `ToolWikiWritePage` / `ToolWikiFlagIssue` 等常量 |
| `resolve_path` 权限层 | `tools/scope_authorization.go`：管的是**数据范围**——`searchTargetsAllowKnowledgeID` 判定 Agent 可访问哪些知识库/文档/标签。层的位置一致：工具执行的必经之路；范围对象不同（文件系统 vs 租户数据） |
| grep/read 输出上限 | `tools/truncate.go`（s11 对照）+ 各工具自己的 limit 参数 |
| 敏感文件隐身 | WeKnora 场景里对应「租户隔离」：查询结果只含本租户数据，他租户数据连存在性都不暴露——`*_scope_test.go` 家族测的就是这个 |

**教学版砍掉了什么**：按 agent 类型/KB 能力动态挑选工具集（教学版全量注册）、
工具级 ctx 超时与取消传播、MCP 外部工具接入、bash 沙箱化。

## 运行方法

```bash
cd C:\Desktop\Project\llm-wiki-learning\s08_file_tools_permissions

# 真实 LLM
python code.py "executeLoop 定义在哪个文件？循环的推进条件是什么？"

# Mock：覆盖全部工具 + 三类权限拒绝（工具真实执行）
python code.py --mock
```

## 示例输出（--mock，节选）

```
[工具] grep({"pattern": "func .*executeLoop", "path": "internal/agent", "include": "*.go"})
[输出] engine.go:351: func (e *AgentEngine) executeLoop(

[工具] read_file({"path": "internal/agent/../../.env"})
[输出] [拒绝] .env 匹配敏感文件清单，连读都不允许（安全红线）。
[工具] write_file({"path": "C:\\Desktop\\Project\\WeKnora\\hack.txt", ...})
[输出] [拒绝] 写入被拒：... 被分析项目（SOURCE_ROOT）是只读的，一个字节都不许改。

[工具] write_file({"path": "workspace/logs/s08-scratch.md", ...})
[输出] [写入] C:\Desktop\Project\llm-wiki-learning\workspace\logs\s08-scratch.md（116 字符）
```

验证后确认：`WeKnora\hack.txt` 不存在，workspace 笔记内容正确。

## 局限性

- bash 逃生舱仍是文本匹配红线，决意绕过是可能的——生产级方案是沙箱或去掉 bash；
- 权限是「根目录白名单」粒度，没有更细的 per-path 规则（如 workspace/state 只许
  流水线写）；
- 符号链接指向根外时 `resolve()` 会暴露真实路径并被拒——正确但报错信息可能困惑；
- 还没有 Wiki 意识：问答仍从零 grep 源码，s01–s06 建的 Wiki 没被用上——s09 解决。

## 下一阶段（s09）

Wiki 问答 Agent：新增 wiki 导航工具（list_wiki_pages / read_wiki_page /
search_wiki），回答优先级 Wiki → Wiki 引用的源码 → 额外源码搜索；
回答包含结论、调用链、相关页面、源文件引用、不确定事项。
对照 `wiki-qa` agent 类型、`wiki_read_source_doc.go`、`chat_pipeline/wiki_boost.go`。
