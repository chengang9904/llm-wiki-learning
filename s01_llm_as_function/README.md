# s01：结构化 LLM 调用 —— 「LLM 即函数」

## 本阶段目标

不写循环、不给工具。只实现一个函数：

```
extract_structured(源文件) ──▶ {summary, key_symbols, responsibilities, references}
```

给定一个源文件的内容和一个模板 prompt，让 LLM 返回结构化 JSON，
用手写 schema 校验它，校验失败就把错误信息喂回去重试（最多 2 次），
内容超过 32768 字符则截断。

## 上一阶段的问题

这是起点，没有上一阶段。但有一个「上一种直觉」要纠正：很多人第一反应是
「让 LLM 自己去读项目、自己决定怎么总结」——那是 Agent 的做法（s07 才开始）。
对「给每个文件生成摘要」这种**批量、可重复、结构可预知**的工作，
正确做法是把 LLM 降级为一个函数：代码掌握全部控制流，模型只做一步文本 → 结构的转换。

## 为什么「LLM 即函数」是流水线的基石

流水线的四个承诺——幂等可重跑、状态外置、成本可控、失败隔离——每一个都
依赖「LLM 调用是一个行为可预期的函数」：

- **输入确定**：prompt 是模板 + 文件内容，同样输入基本得到同样含义的输出；
- **输出可校验**：返回 JSON，schema 校验能机械地判断成败，失败可以机械地重试；
- **成本有上限**：一次调用 = 一份截断过的内容 + 一份固定模板，账单可估算；
- **失败可隔离**：一个函数调用失败，记下来重跑即可，不会污染别的调用。

如果这一步让模型自由发挥（比如返回自由格式 Markdown），后面 s03 的归并、
s04 的去重和链接、s05 的增量更新都没有可靠的结构化数据可用——整条流水线塌掉。

## JSON 修复的常见坑

LLM 输出非法 JSON 的高频原因（WeKnora 的 `json_repair.go` 处理了十几种，教学版处理前两种）：

1. **Markdown 围栏**：明确说了「只输出 JSON」，模型仍然包一层 ```` ```json ````；
2. **前后多余文字**：「好的，以下是分析结果：{...}」；
3. **字符串值里的裸换行**：JSON 字符串不允许字面换行符，必须 `\n`——
   这是长文本字段最常见的解析失败原因，所以 prompt 里的 JSON Formatting Rules
   专门写了这条（直接借鉴 `prompts_wiki.go`）；
4. 尾逗号、单引号、未转义引号（教学版不处理，交给重试）。

处理策略分两层：**先机械修复**（剥围栏、取第一个 `{` 到最后一个 `}`），
**修不好再带错误重试**——把解析/校验错误原文追加进对话，让模型自己改。
重试是有上限的（2 次），超限就明确失败，绝不无限循环。

## 代码解析（code.py，约 250 行）

| 部分 | 说明 |
|---|---|
| `MAX_CONTENT_CHARS = 32768` | 成本红线，对照 WeKnora 的 `maxContentForWiki` |
| `is_sensitive_path` + `SENSITIVE_PATTERNS` | 安全红线：`.env`、`*.pem` 等连读都拒绝 |
| `load_env` | 手写 .env 加载（KEY=VALUE），本目录 → 项目根目录 |
| `SYSTEM_PROMPT` / `EXTRACT_PROMPT` | 模板 prompt：字段定义 + JSON Formatting Rules |
| `extract_json_text` | 机械修复：剥围栏、取花括号区间 |
| `validate_extraction` | 手写 schema 校验，返回错误列表（空 = 通过） |
| `OpenAILLM` / `MockLLM` | 真实调用 / 脚本化 Mock（第 1 次响应故意非法） |
| `extract_structured` | 核心控制流：读 → 截断 → 调用 → 校验 → 带错误重试 |

注意 `extract_structured` 里模型只出现在 `llm.complete()` 一行——
**谁掌握控制流**，这就是 s01 与 s07 的本质区别。

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版 |
|---|---|
| `extract_structured` 的「模板 + 调用 + 校验」 | `generateWithTemplate`（`internal/application/service/wiki_ingest.go:2253`）用 Go template 渲染 prompt 后调用 chat 模型 |
| `MAX_CONTENT_CHARS = 32768` | `maxContentForWiki = 32768`（`wiki_ingest.go:39`），在 `mapOneDocument` 里按 rune 截断（`wiki_ingest_batch.go:1154`） |
| `extract_json_text` 剥围栏取花括号 | `RepairJSON`（`internal/agent/tools/json_repair.go`），处理围栏、裸换行、尾逗号等十几种畸形 |
| prompt 里的 JSON Formatting Rules | `internal/agent/prompts_wiki.go` 各 prompt 的同名小节（"Do NOT use literal newlines inside JSON string values"） |
| 带错误重试 2 次 | `mapOneDocument` 的 pass 0 失败后回退 legacy 提取器（`extractEntitiesAndConceptsNoUpsert`），再失败则整篇文档失败进重试队列 |

**教学版砍掉了什么**：Langfuse 追踪 span、按 rune（而非字符）截断的 Unicode 严谨性、
多语言 prompt、pass 0 / legacy 双提取器回退、图片 markup 检测（`hasSufficientTextContent`
防止对纯扫描件幻觉提取）、per-model 并发配额与 429 退避。这些都是生产化工作，
不影响「LLM 即函数」这个核心概念。

## 运行方法

```bash
cd C:\Desktop\Project\llm-wiki-learning\s01_llm_as_function

# 方式一：Mock LLM（无需 API Key，演示失败 → 重试路径）
python code.py C:\Desktop\Project\WeKnora\internal\agent\engine.go --mock

# 方式二：真实 LLM（先配置 .env）
copy .env.example .env    # 填入 OPENAI_API_KEY / OPENAI_BASE_URL / MODEL_ID
pip install -r ..\requirements.txt
python code.py C:\Desktop\Project\WeKnora\internal\agent\engine.go

# 验证安全红线：应当被拒绝
python code.py C:\Desktop\Project\WeKnora\.env --mock
```

## 示例输出（--mock）

```
[调用] 第 1 次 LLM 调用 ...
[失败] 第 1 次尝试未通过：字段 "responsibilities" 缺失或不是数组; 字段 "references" 缺失或不是数组
[调用] 第 2 次 LLM 调用 ...
[通过] schema 校验通过（第 2 次尝试）

=== 结构化提取结果 ===
{
  "summary": "engine.go 实现 WeKnora 的 ReAct Agent 引擎。...",
  "key_symbols": ["AgentEngine", "executeLoop", "runReActIteration", ...],
  "responsibilities": ["驱动 ReAct 主循环并在每轮之间维护 Agent 状态", ...],
  "references": ["internal/agent/tools", "internal/types"]
}
```

（`engine.go` 约 27KB，未触发截断；换 `wiki_ingest.go`（约 108K 字符）运行即可看到
`[截断] 内容 107977 字符 > 上限 32768，已截断`。）

Mock 的第 1 次响应故意包了 Markdown 围栏且缺少两个字段：围栏被 `extract_json_text`
机械修复掉（所以不会报 JSON 解析错误），缺字段则被 schema 校验抓住，
错误清单喂回模型后第 2 次通过——一次运行同时演示了「机械修复」和「带错误重试」两层。

## 局限性

- 一次只处理一个文件，没有批量、没有状态——重跑就是完整重做；
- 提取结果只打印到终端，没有落盘，更没有变成 Wiki 页面；
- 失败重试上限后直接退出进程——在批处理场景这会让一个坏文件毁掉整批。

## 下一阶段（s02）

把提取结果渲染成真正的 Wiki 页面：带 frontmatter（title / type / status / sources /
updated_at），重要事实必须有来源引用；同时引入文件扫描器——目录排除清单
（`.git`、`node_modules`、`frontend/`、`web/`……）和敏感文件红线从「拒绝单个文件」
升级为「批量扫描时的系统性过滤」。
