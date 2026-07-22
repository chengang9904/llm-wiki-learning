# s12：Fixer Agent 与完整整合 —— 课程收官

## 本阶段目标

```
python code.py init      # 初始化 workspace                 （代码）
python code.py ingest    # s03 map-reduce → s04 后处理       （流水线）
python code.py update    # s05 git diff → s04 后处理         （流水线）
python code.py lint      # s06 质量检查 → issues.json        （流水线）
python code.py query     # 基于 Wiki + 源码回答（s11 全能力） （Agent）
python code.py fix       # 消费 issues.json 逐条修复          （Agent）
python code.py status    # 汇总全部状态                       （代码）
```

**命令行入口本身就是双范式的分界线。**

## 上一阶段的问题

s06 的 lint 只发现不修复，issues.json 里躺着一堆 open issue 没人处理；
十一个阶段各自可运行，但没有一个统一入口把「生成 → 检查 → 修复 → 问答」
连成日常工作流。

## 新增机制

### 1. Fixer：双范式的交接点落成代码

`cmd_fix` 的结构值得逐字读：

```python
for issue in open_issues:          # 外层循环是代码：逐条取、按严重度排序、
    ...                            #   --limit 限额、try/except 失败隔离
    agent_loop(task, llm, FIXER_SYSTEM_PROMPT, FIXER_TOOLS, ...)   # 内层判断是 Agent
    after = ...                    # 事后核对：Agent 是否真的 update_issue 了
    if after["status"] == "open":  #   ——不信任自觉，验证状态
        results["unresolved"] += 1
```

外层是流水线原则（确定性迭代、失败隔离、限额、状态核对），内层是 Agent 原则
（这条 issue 该修、该弃、还是该报告——判断题）。全课程练的就是**在哪个边界上交接**，
这里是最终答案的样板。

Fixer 的流程纪律（写在 system prompt）：先 `load_skill("fix-wiki-page")` 拿决策树
（s11 提前入库的那份技能在此兑现）→ **必须重读来源再下结论**（绝不允许只看
issue 描述就改页面）→ 三种出路，必须以 `update_issue` 收尾：
resolved（修复）/ resolved+页面标 deprecated / ignored（原因必填）。

### 2. 修页面的唯一写入手段：replace_in_page

对照 `wiki_replace_text.go`：`slug + old_text + new_text` 的精确替换——
old_text 找不到时返回「必须逐字复制页面原文」（对照生产版 :84 的同款报错，
**这个报错是喂给模型的操作指导**）；出现多次时拒绝（无法唯一定位）。
替换成功自动刷新 `updated_at`、写修改日志、同步 pages.json 的来源列表。
不给 Fixer 裸的 write_file——领域动作用领域工具，权限和审计都在工具里。

### 3. issue 三件套

`read_issue / update_issue / flag_issue`，对照 `wiki_read_issue / wiki_update_issue /
wiki_flag_issue`。update 的状态枚举 `resolved / ignored / pending` 与生产版
（`wiki_update_issue.go:32`）完全一致。`flag_issue` 同时注册给 query Agent——
s09 遗留的「发现不一致只能写在回答里」在此闭环：问答顺手 flag，Fixer 统一消费，
lint 与 Agent 两个来源的问题走同一条处理线。

### 4. 流水线命令 = 子进程调度兄弟阶段

`ingest/update/lint` 不重写实现，子进程调用 s03/s04/s05/s06 的 code.py——
它们各自的幂等、失败隔离、断点续跑原样生效。整合层只做调度。
（教学动机：diff 各阶段仍然干净；工程动机：一份实现一处维护。）

## 运行验证（真实发生的闭环）

```
fix --mock --limit 3：
  issue #2 [error] dead_source_ref：
    load_skill(fix-wiki-page) → read_wiki_page → glob 确认来源确实不存在
    → replace_in_page 删除失效引用行（46 → 0 字符，updated_at 刷新）
    → update_issue(resolved)
  issue #3/#4 [info] 结构性问题：按决策树 → update_issue(ignored, 建议重跑后处理)

随后 lint（级联效应，全部符合预期）：
  #7 missing_sources 新增 —— 正是 Fixer 在 note 里预告的后续
  #6 stale_draft auto_resolved —— updated_at 被修复动作刷新
  exit 0 —— error 级 issue 已清零

tests/smoke.py：11/11 通过（全课程 --mock 无 Key 可跑）
```

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版 |
|---|---|
| `fix` 命令 + FIXER_SYSTEM_PROMPT | `builtin-wiki-fixer`（`types/custom_agent.go:30`）；:554 的注释特意说明它**被排除出普通 agent 列表**——修复是运维动作不是聊天选项，教学版同样把 fix 做成独立命令而非 query 的分支 |
| `replace_in_page` | `wiki_replace_text.go`：同款参数、同款「copy exactly」报错、同款唯一性要求 |
| issue 三件套 | `wiki_flag_issue / wiki_read_issue / wiki_update_issue`；状态枚举逐字对齐 |
| 外层代码循环逐条派发 | WeKnora 的 fixer 由 lint 报告/用户触发，逐 issue 处理；页面存活校验、租户隔离、审计日志包在外面 |

**教学版砍掉了什么**：修复的 dry-run/审批流、页面版本历史与回滚、
并发修复的页面锁、修复动作的审计事件。

## 局限性

- Mock Fixer 是按 issue 类型写死的脚本——真实 LLM 才会做出「grep 同名符号改指
  新位置」这类有创造性的修复（决策树里有，mock 走不到）；
- 逐条串行修复，一条一个 Agent 会话——生产上高频同类 issue 应该批量归并后处理；
- `ignored` 的结构性问题需要人（或 cron）记得重跑 ingest/postprocess——
  没有自动的后续动作编排。

## 课程完结

至此六命令俱全，两个范式在同一个 CLI 里各就各位。
双范式对比表与 WeKnora 的生产化工作清单见根 README 的收尾章节。
