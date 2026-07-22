# s11：上下文工程 —— Skill 按需加载 + 历史压缩

## 本阶段目标

上下文是 Agent 最贵、最稀缺的资源。s10 用子 Agent 隔离了「过程」，还剩两个漏洞，
本阶段各给一个机制：

| 漏洞 | 机制 | 效果 |
|---|---|---|
| 方法论全塞 system prompt，每轮都付这笔 token | **Skill 按需加载**：启动只载「名称+一行说明」的索引，需要时 `load_skill(name)` 拿全文 | 8 份方法论常驻成本 ≈ 8 行索引 |
| 父上下文随轮次线性增长，满了任务就断 | **历史压缩**：超阈值时把中段历史压成备忘录（任务/关键发现/未完成事项），压缩后**继续不重扫** | demo 里 ~6037 tok → ~3150 tok |

另把 s07 起的「全局 4000 字符截断」升级为**分工具预算表**（`TOOL_OUTPUT_BUDGETS`）：
read_file / read_wiki_page 是证据，多留（8000）；list_files 是清单，少留（3000）。

## 上一阶段的问题

s10 的父 Agent 每轮都携带全部方法论提示 + 全部历史。长调查跑到十几轮时：
(a) 大部分 prompt 开销花在当前任务用不到的指导上；(b) 上下文一旦满，
之前所有探索成果随进程一起报废——没有任何「存档」机制。

## 新增机制

### 1. Skill：知识的懒加载

`skills/` 目录 8 个技能文件（本阶段随代码一起交付），frontmatter 只有两行：

```markdown
---
name: trace-call-chain
description: 跨模块调用链追踪的方法论：分段、子 Agent 派发、结构化汇合。
---
（正文：完整方法论，含 WeKnora 特定的锚点提示）
```

启动时 `load_skills_index()` 只解析 frontmatter 进 system prompt；
`load_skill(name)` 才读正文。**判断「现在需要哪份方法论」的是模型**——
这延续了 Agent 篇的一贯原则：代码提供机制（索引/加载），模型做决策（何时加载哪个）。

八个技能：analyze-go-project / analyze-vue-project / analyze-python-service /
analyze-grpc-service / analyze-database / trace-call-chain / verify-sources /
fix-wiki-page（最后一个是 s12 Fixer 的工作手册，提前入库）。
技能正文不是泛泛的「best practices」——每份都带 WeKnora 的具体锚点
（container.go 是地图、迁移有两套、frontend 目录被排除但 read_file 不受影响……），
这是「技能」区别于「提示词模板」的地方：它携带**领域经验**。

### 2. 历史压缩：把上下文当环形缓冲区

每轮调用前估算（`chars//3` 粗估）；超阈值时：

```
[system, user(任务), ...中段（压缩掉）..., 尾部若干条]
        ↓ 一次 LLM 调用：中段 → 工作备忘录
[system, user(任务 + 备忘录), 尾部若干条]
```

三个关键细节：

- **备忘录必须保住三样东西**（丢了任务就断）：原始任务、已确认的关键发现
  （**带文件:行号**——这是压缩后不必重扫的资本）、未完成事项。压缩 prompt
  明说不要保留工具原始输出——原始输出的价值已经被「发现」蒸馏走了；
- **尾部切点必须安全**：不能把 assistant 的 tool_calls 和它的 tool 结果拆开
  （OpenAI API 直接报错）。`_safe_tail_start` 从候选切点向后走，跳过 role:tool
  的消息——对照 `sanitize_messages.go` 的消息序列修理；
- **压缩后能继续**：注入语明确「关键发现已核实过，直接使用，不要重扫」。
  demo 的第 4 轮就是证明：只做了一次小 grep 补缺口，没有重读任何大文件。
  同时最终回答的「不确定事项」里诚实标注了哪条事实来自压缩前、未二次核实——
  压缩是有损的，损失要可见。

### 3. 循环的唯一改动

s07 承诺循环不重写。s11 是六个阶段里对循环动作最大的一次，也只是在
`llm.chat` 之前插了三行压缩钩子——检查预算、必要时换掉 messages。
循环的骨架（调用→存回→分发→喂回）仍然一个字没变。

## 代码解析（相对 s10 的增量）

| 新增/变化 | 说明 |
|---|---|
| `load_skills_index` / `tool_load_skill` | frontmatter 索引 + 全文懒加载 |
| `build_system_prompt` | 技能索引注入 + 「不要凭空发明方法论」指令 |
| `TOOL_OUTPUT_BUDGETS` | 分工具截断预算，`execute_tool` 统一执行 |
| `estimate_tokens` | chars//3 粗估（生产版用真实 tokenizer） |
| `_safe_tail_start` | 安全切点：不拆散 tool_calls 配对 |
| `compress_history` | 中段序列化（每条限长）→ 压缩调用 → 重组消息 |
| `agent_loop` 压缩钩子 | 每轮调用前三行 |
| 子 Agent 工具集 + load_skill | 子 Agent 也能按需加载技能 |

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版 |
|---|---|
| `skills/*.md` frontmatter | `skills/preloaded/<name>/SKILL.md`：同款 name+description frontmatter（Anthropic 技能格式）；技能可携带脚本（data-processor/scripts/） |
| `load_skill` | `tools/skill_read.go`；启动同样只注入索引 |
| `compress_history` | `agent/memory/consolidator.go`：`DefaultConsolidationThreshold = 0.5`（占上下文一半触发，:20），压缩目标为阈值的 60%（:110）；同样保留近期消息、压缩早期历史 |
| `_safe_tail_start` | `tools/sanitize_messages.go`：消息序列修理（孤儿 tool 结果、未闭合调用） |
| `TOOL_OUTPUT_BUDGETS` | `tools/truncate.go` + 各工具 limit 参数 |
| `estimate_tokens` | 生产版有真实 token 计数（engine.go 每轮日志里的 currentTokens） |

**教学版砍掉了什么**：真实 tokenizer、多级记忆（consolidator 之外还有会话持久化）、
压缩失败的降级策略、技能内嵌脚本的执行。

## 运行方法

```bash
cd C:\Desktop\Project\llm-wiki-learning\s11_context_engineering

# 真实 LLM（默认阈值 24000，长任务自然触发）
python code.py query "从上传一个文档到向量入库，WeKnora 的完整处理链路是什么？"

# Mock + 低阈值强制触发压缩（决策脚本化，工具与压缩真实执行）
python code.py query "Agent 引擎的迭代控制与收尾机制是什么？" --mock --compress-threshold 3000
```

## 示例输出（--mock --compress-threshold 3000，节选）

```
技能索引（8 个，只载名称+一行说明）：analyze-database, analyze-go-project, ...
───── 第 1/20 轮（~309 tok） ─────
[工具] load_skill({"name": "trace-call-chain"})
───── 第 2/20 轮（~559 tok） ─────
[工具] read_file({"path": "internal/agent/engine.go", "offset": 340, "limit": 300})
───── 第 3/20 轮（~3300 tok） ─────
[工具] read_file({"path": "internal/agent/finalize.go", ...})
    [压缩] ~6037 tok > 阈值 3000 → 压缩后 ~3150 tok（8 条 → 4 条，保留尾部 2 条）
───── 第 4/20 轮（~3150 tok） ─────
[模型] 凭备忘录里的发现继续：只差 handleMaxIterations 的精确位置，小 grep 核实，不重读大文件。
[工具] grep({"pattern": "func .*handleMaxIterations", ...})
```

最终回答的「不确定事项」标注：「engine.go:435-448 是 iterOutcome——此条来自
压缩前的阅读，未在压缩后二次核实」——压缩的有损性是显式的。

## 局限性

- chars//3 是粗估，中英文/代码比例变化时偏差可达 ±30%；
- 压缩本身要花一次 LLM 调用——阈值设太低会频繁压缩，得不偿失
  （对照 consolidator 用 0.5 的高水位 + 一次压到 60%，避免抖动）；
- 备忘录质量完全取决于压缩 prompt 与模型——关键发现被压丢时，模型会在
  错误的自信下继续（所以要求「不确定事项」披露未二次核实的事实）；
- 技能是静态文件，没有版本/生效条件管理。

## 下一阶段（s12）

Fixer Agent 与完整整合：`fix` 命令读取 state/issues.json，Agent 逐条判断
（重读来源 → 修复 / 标 deprecated / 报告无法修复，正好用上 fix-wiki-page 技能）；
六命令整合 init / ingest / update / lint / query / fix / status——
流水线与 Agent 在一个 CLI 里各就各位；根 README 收尾双范式对比表。
对照 `builtin-wiki-fixer`、`wiki_flag_issue / wiki_read_issue / wiki_update_issue`、
`wiki_replace_text.go`。
