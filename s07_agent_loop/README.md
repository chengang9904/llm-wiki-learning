# s07：最小 Agent Loop —— 「模型即决策者」

## 本阶段目标

Agent 篇开篇。用不到 250 行实现一个只有 **bash 一个工具**的完整 Agent：

```python
while True:
    response = call_llm(system_prompt, messages, tools)
    messages.append(response)
    if response 不含 tool calls:
        return response.text          # 模型认为答完了 → 循环结束
    messages.append(execute_tool_calls(response.tool_calls))
```

验证任务：`python code.py "WeKnora 用了哪些技术栈？各顶层目录职责是什么？"`
——注意代码里**没有任何针对这个问题的逻辑**。先 ls 还是先读 go.mod、
要不要看 CLAUDE.md、什么时候停：全是模型的决策。

## 与 s01 的本质区别：谁掌握控制流

| | s01（LLM 即函数） | s07（模型即决策者） |
|---|---|---|
| 读哪个文件 | 代码指定 | 模型决定 |
| 调几次 LLM | 代码写死（1 + 重试） | 模型决定（发不发 tool call） |
| 什么时候结束 | 一次调用即结束 | 模型不再调工具时结束 |
| 失败怎么办 | 代码带错误重试 | 模型看到错误输出后自己调整策略 |
| 代码的角色 | 编排者 | **Harness**：忠实执行、守红线、管上下文 |

调用的是同一套 chat completions API——差别只在**代码把返回里的 tool_calls
当什么**：s01 里模型输出是「结果」，s07 里模型输出是「下一步指令」。

最后一行的差别最能说明范式：s01 的失败靠代码写好的重试路径；s07 里
`cat .env` 被拒绝后，模型下一轮**自己**决定绕开——自愈不是写出来的，
是「把结果喂回去」这个结构自带的。

## Loop 为什么这么简单（而且必须保持简单）

因为复杂度全都长在循环**外面**：
- 能力增长 = 加**工具**（s08 文件工具、s09 wiki 工具、s10 todo/子 Agent）；
- 智力增长 = 换更强的**模型**、更好的 **system prompt**、按需加载的 **skill**（s11）；
- 循环本身从 s07 到 s12 **一个字不改**。

循环一旦掺进业务逻辑（「如果模型连续两次 grep 就强制它读文件」这类小聪明），
每加一个工具都要重审循环，Agent 就不可维护了。WeKnora 的 `executeLoop` 五年内
可以不动，动的是 tools/ 目录——同一个道理。

## 护栏（Harness 的另一半职责）

| 护栏 | 实现 | 对照 |
|---|---|---|
| 最大迭代数 | `for round_no in range(1, max_iterations+1)`，耗尽时给出明确交代 | `MaxIterations`（engine.go:386）、`handleMaxIterations`（finalize.go:160） |
| 敏感文件红线 | 命令文本匹配 `.env/*.pem/credentials...` 直接拒绝，**拒绝理由作为工具输出返回** | system prompt 声明 + 执行层强制，双保险 |
| 破坏性命令黑名单 | `rm -rf` / `git push` / `shutdown` 等模式拒绝 | s08 升级为完整权限层 |
| 工具输出截断 | 单条 4000 字符，防一条 cat 撑爆上下文 | s11 系统化为上下文工程 |
| 命令超时 | 30s | tools 层的 ctx 超时 |

红线设计的关键：**拒绝不是异常，是信息**。返回「[拒绝] …安全红线禁止访问」
让模型知道为什么失败、从而调整策略——demo 里模型试探 `cat .env` 被拒后，
第 3 轮照常收敛作答。

## 代码解析

| 部分 | 说明 |
|---|---|
| `TOOLS` | OpenAI tool schema：一个 `bash`，参数就一个 `command` |
| `find_bash` / `run_bash` | Windows 陷阱：PATH 里的 bash 可能是 System32 的 WSL 存根，要定位 Git Bash 真身；执行 + 红线 + 截断 + 超时 |
| `SYSTEM_PROMPT` | 角色 + 探索策略提示 + 安全红线声明 + 回答要求 |
| `agent_loop` | 上面那 8 行伪代码的忠实展开；assistant 消息（含 tool_calls）必须原样放回历史，每个 tool call 的结果以 `role:"tool"` + `tool_call_id` 喂回 |
| `MockLLM` | 脚本化决策序列（**bash 是真实执行的**，只有决策是预演的）：ls → 并行三调用（含一次被拒的 `cat .env`）→ 收敛作答；`--mock-loop` 永不收敛演示护栏 |

OpenAI 与参考项目 Anthropic SDK 的消息结构差异（根 README 有完整对照表）：
tool 调用在 `message.tool_calls`（不是 content 块），工具结果是独立的
`{"role": "tool", "tool_call_id": ...}` 消息（不是 user 消息里的 `tool_result` 块）。

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版（`internal/agent/engine.go`） |
|---|---|
| `agent_loop` 的 for 循环 | `executeLoop`（:351）：`for state.CurrentRound < e.config.MaxIterations` |
| 循环体一轮 | `runReActIteration`（:457）：think → analyze → act → observe，act/observe 分别在 `act.go` / `observe.go` |
| 「有无 tool_calls」决定去留 | `iterOutcome` 哨兵（:435）——用显式的 continue/break/return 语义代替裸返回值 |
| 耗尽护栏的兜底文案 | `handleMaxIterations`（`finalize.go:160`）：用已收集的信息强行生成一个答案（比教学版的道歉信更进一步） |

**生产版在骨架外还包了什么（教学版全部砍掉）**：每轮的 token 估算与上下文预算
（engine.go 里的 currentTokens 日志）、memory consolidator（`agent/memory/`，
长对话压缩）、Langfuse span 追踪（每轮 think/act/observe 都有 span）、
事件总线流式输出（think 的 token 逐个推给前端）、pinned mentions、
图片描述注入。骨架不变——这正是「后续只加工具与状态，不重写循环」的底气。

对照参考项目：learn-claude-code **v0**（`v0_bash_agent.py`，同样只有 bash）、
**v1**（`v1_basic_agent.py`，加了基础文件工具——即本课程的 s08）。

## 运行方法

```bash
cd C:\Desktop\Project\llm-wiki-learning\s07_agent_loop

# 真实 LLM（需 .env）
python code.py "WeKnora 用了哪些技术栈？各顶层目录职责是什么？"

# Mock：脚本化决策 + 真实 bash 执行，无需 API Key
python code.py --mock

# MaxIterations 护栏演示：mock 永不收敛，3 轮后强制停止
python code.py --mock-loop --max-iterations 3
```

## 示例输出（--mock，节选）

```
───── 第 2/15 轮 ─────
[模型] 读 go.mod 和 CLAUDE.md 确认技术栈；顺便试试读 .env（会被拒绝）。
[工具] bash$ head -n 15 go.mod
[输出] module github.com/Tencent/WeKnora
       go 1.26.0 ...
[工具] bash$ cat .env
[输出] [拒绝] 命令涉及敏感文件（.env / 密钥 / 凭据），安全红线禁止访问。

───── 第 3/15 轮 ─────
[模型] （mock 最终回答）……注意第 2 轮里 cat .env 被执行层拒绝了——
       红线在 Harness，不靠模型自觉。
```

## 局限性

- bash 是个粗粒度工具：模型要自己拼 `grep -rn` / `find` 命令，容易写错、
  输出难控制——s08 换成原子化文件工具；
- 红线靠命令文本匹配，能被绕过（`cat .e*v`）——s08 升级为路径规范化后的
  权限层，在文件访问的必经之路上检查；
- 没有 Wiki 意识：问「文档上传流程」它只会 grep 源码，不会先查我们
  s01–s06 建好的 Wiki——s09 解决；
- 长任务会撑爆上下文——s10 子 Agent、s11 压缩。

## 下一阶段（s08）

原子文件工具与权限边界：list_files / read_file / grep / glob 统一注册表；
权限层——SOURCE_ROOT 只读、只允许写 llm-wiki-learning/ 内部、敏感文件连读都拒、
路径规范化防穿越（Windows 路径陷阱）、bash 黑名单 + 超时保留。
对照 `tools/registry.go`、`definitions.go`。
