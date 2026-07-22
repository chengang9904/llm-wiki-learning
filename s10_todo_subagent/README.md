# s10：Todo 与子 Agent

## 本阶段目标

让 Agent 能胜任**跨模块调用链追踪**这类长任务。长任务的两个瓶颈，各给一个工具：

| 瓶颈 | 工具 | 机制 |
|---|---|---|
| **忘**：十几轮后忘了查到哪一步 | todo_write / todo_read / todo_update | 显式计划，四状态推进（pending / in_progress / completed / blocked），持久化到 state/session.json |
| **胀**：每步的原始输出都堆在同一个上下文 | spawn_subagent | 独立上下文的子 Agent，只返回结构化结论 `{summary, important_files, call_chains, uncertainties}` |

验证任务：`python code.py query "从上传一个文档到向量入库，WeKnora 的完整处理链路是什么？"`

## 上一阶段的问题

s09 的问答 Agent 在单一上下文里干所有事。问「完整上传链路」这种横跨
handler / service / docreader / embedding / 向量库五个模块的问题，
每一段的 grep 和 read_file 原始输出都留在历史里——第 15 轮时模型既记不清
计划，上下文也快满了。

## 新增机制

**1. 计划由模型生成，代码不写死。** `todo_write` 只负责存取与校验——
把任务拆成几步、每步叫什么，完全是模型的决策。对照 `tools/todo_write.go`
最有教学价值的一点：**纪律写在工具描述里，不写在代码里**——
「同一时刻只保持一个 in_progress」「完成才标 completed」「做不下去标 blocked
并写原因」都是喂给模型的文字，模型自愿遵守；代码没有任何强制。
这是 Agent 范式的一贯姿势（同 s09 的三级瀑布）：策略进 prompt，能力进工具。

todo 列表持久化到 `state/session.json`——Agent 的工作状态同样遵循流水线篇的
「状态外置」原则，跑完后可以事后检查（验证输出的最后一节就是它）。

**2. 子 Agent = 上下文的进程隔离。** `spawn_subagent(task)` 的三条硬规则：
- **不继承对话历史**：子 Agent 只拿到任务描述——所以工具描述里强调
  「任务必须自包含」，逼着父 Agent 把上下文压缩进任务文本；
- **工具集受限**：只读探索工具，没有 todo（子 Agent 只有一个任务）、
  没有 spawn_subagent（深度=1，防递归失控）——用 `execute_tool(name, args, allowed)`
  的白名单参数实现，同一注册表、不同可见集；
- **返回结构化结论**：子 Agent 的 system prompt 要求最终输出固定 JSON；
  harness 解析校验，解析失败就把原文包进 summary（劳动不白费）。

效果：子 Agent 探索了 3 轮、产生了几 KB 的 grep/read 原始输出，
父 Agent 的上下文只增加一个几百字符的 JSON。**父上下文涨结论，不涨过程。**

**3. 父 Agent 的角色转变：执行者 → 调度者。** system prompt 明确：
每段独立调查交给子 Agent，父 Agent 只做规划、派发、综合；
同时提醒「简单问题不需要 todo 也不需要子 Agent」——工具是选项不是仪式，
什么时候用还是模型的判断。

## 代码解析（相对 s09 的增量）

| 新增/变化 | 说明 |
|---|---|
| `TODOS` + 三个 todo 工具 | 列表覆盖式建立、单项状态更新、渲染回显（模型每次操作都看到全表） |
| `_persist_todos` → session.json | 状态外置 |
| `execute_tool(..., allowed)` | 注册表不变，增加调用方可见集——父全量、子受限 |
| `run_subagent` | 独立 messages、独立 system prompt、独立轮次预算（8）、缩进打印、JSON 解析校验 + 兜底 |
| `tool_spawn_subagent` | 把 run_subagent 包装成普通工具——**子 Agent 对循环而言只是一个耗时较长的工具调用** |
| `SUBAGENT_LLM_FACTORY` | 真实模式复用同一 OpenAI 客户端；mock 模式按任务分流脚本 |
| `MockParentLLM` / `MockChildLLM` | 父：建计划→派发→blocked 演示→综合；子：真实 grep/read 后返回结构化 JSON |

最后一点值得强调：spawn_subagent 在父循环眼里**就是一个工具**——
循环依然一个字没改。递归的复杂度被工具边界完全吸收。

## 对照

| 教学版 | 对照 |
|---|---|
| todo 三件套 | `tools/todo_write.go`：WeKnora 同样用覆盖式列表 + 状态推进，工具描述里写满使用纪律（「一次只一个 in_progress」原文可查:106）；状态集 pending/in_progress/completed（教学版按课程规格多一个 blocked） |
| spawn_subagent | learn-claude-code **v3**（`v3_subagent.py`）：同样的「独立上下文 + 受限工具集 + 结构化返回」。WeKnora 没有通用子 Agent——它的 Agent 面向交互问答，深探索靠专用工具（wiki_read_source_doc 等）收敛上下文，且有 memory consolidator 兜长对话（s11 对照） |
| 结构化返回 schema | 课程规格指定的 `{summary, important_files, call_chains, uncertainties}`——注意它和 s03 流水线 map 的结构化输出是同一个思想：**跨上下文传递的信息必须结构化**，无论传给代码还是传给另一个 Agent |

## 运行方法

```bash
cd C:\Desktop\Project\llm-wiki-learning\s10_todo_subagent

# 真实 LLM
python code.py query "从上传一个文档到向量入库，WeKnora 的完整处理链路是什么？"

# Mock：父/子 Agent 决策脚本化，工具真实执行
python code.py query "从上传一个文档到向量入库，WeKnora 的完整处理链路是什么？" --mock
```

## 示例输出（--mock，节选）

```
[工具] todo_write({"items": ["定位上传入口…", "追踪 docreader…", "追踪切分→Embedding→向量入库", "综合…"]})
[工具] spawn_subagent({"task": "在 WeKnora 中定位「上传文档」的入口调用链…"})
    ┌─ 子 Agent 启动：…
    │ [子工具] grep({"pattern": "func .*CreateKnowledgeFromFile", ...})
    │ [输出] application/service/knowledge_create.go:27: func (s *knowledgeService)…
    │ [子工具] read_file({"path": "internal/handler/knowledge.go", "offset": 310, ...})
    └─ 子 Agent 返回（summary 118 字符，文件 2，链 1）
[输出] {"summary": "上传入口是 KnowledgeHandler.CreateKnowledgeFromFile…",
        "important_files": ["internal/handler/knowledge.go:310", ...], ...}

═════ 最终计划状态（state/session.json） ═════
1. ✓ [completed] 定位上传入口：HTTP handler 到 service 层
2. ✓ [completed] 追踪 docreader gRPC 解析在哪里被调用
3. ⊘ [blocked] 追踪切分 → Embedding → 向量入库 —— mock 演示预算内不展开…
4. ► [in_progress] 综合各段成完整链路
```

## 局限性

- 子 Agent 串行执行（父等一个回来才派下一个）——生产系统会并行派发；
- 任务描述是父子间唯一的信息通道，描述写差了子 Agent 就跑偏——真实使用中
  这是最常见的失败模式（提示词工程转移到了「任务写作」上）；
- todo 纪律靠模型自觉，模型可能忘了更新状态——WeKnora 同样如此；
- 父 Agent 自己的上下文仍会随轮次线性增长（每轮 todo 回显也占空间）——
  s11 的压缩解决。

## 下一阶段（s11）

上下文工程：skills/ 按需加载（启动只载名称+一行说明，需要时 load_skill）+
工具返回截断的系统化 + 接近上限时压缩历史（保留任务/关键发现/未完成事项，
压缩后能继续不重扫）。
对照 `skills/`、`skill_read.go`、`truncate.go`、`agent/memory/`。
