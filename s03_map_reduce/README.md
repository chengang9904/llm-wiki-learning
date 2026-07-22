# s03：批量 map-reduce 与幂等状态

## 本阶段目标

把 s02 的「手工指定一个文件」升级为完整的批处理流水线，一条命令处理任意多的文件：

```
python code.py ingest
  └─ scan → manifest 哈希对比（已处理自动跳过）           [幂等]
  └─ 变更文件入队 pending.json（path+hash 去重）          [状态外置]
  └─ while 队列非空：
        认领一批（≤5 个，对照 wikiMaxDocsPerBatch）        [成本可控]
        map    每个文件 → 主题更新（失败记账，不中断整批）  [失败隔离]
        reduce 按 slug 分组 → 读旧页面+新证据 → 归并写页面
        标记 done + 回写 manifest → 状态落盘               [崩溃恢复]
```

**概念转变是本阶段的核心**：s02 是「一个文件 → 一个页面」；s03 是「多个文件的证据 →
归并到同一主题页面」。map 的产物不再是页面，而是 **SlugUpdate**（对某主题的证据补充）；
**reduce 是页面的唯一写入者**。这正是 WeKnora `mapOneDocument` 返回 `[]SlugUpdate`、
由 `reduceSlugUpdates` 统一落库的结构。

## 上一阶段的问题

- scan 的清单没人消费，map 靠手工指定文件；
- 没有状态：重跑就是重做（重复付费），崩溃就前功尽弃；
- 1:1 文件→页面映射：「Agent 引擎」页面应当综合 engine.go + think.go + act.go
  的证据，s02 做不到；
- 失败直接 `sys.exit`——批处理里一个坏文件会毁掉整批。

## 新增机制

**1. manifest 幂等（sources.json）。** 每个文件记 `hash` 与 `processed_hash`，
两者相等 = 已处理，跳过。文件改动后哈希变化，自动重新入队。
入队带去重：同一 `(path, hash)` 不重复入队——对照 `task_pending_ops` 表的 `dedup_key`。

**2. op 队列与失败隔离（pending.json）。** 每个文件一个 op：
`pending → done`，或失败时 `attempts+1`：未超限留在队列自动重试（对照
`requeueFailedOps` 归还 op 且保留 fail_count），超限（教学版 2 次，WeKnora
`wikiMaxFailRetries = 5`）转 `failed` 永久失败——这就是 dead-letter，
`retry` 命令是它的人工重放。**单个文件失败绝不中断整批。**

**3. map 产出 SlugUpdate。** map 的 prompt 把**已有页面 slug 列表**给模型并要求优先复用
——多个文件因此汇入同一主题（对照 WeKnora 把 `oldPageSlugs` 传给提取器）。
每条更新记住自己的 `source_path`：证据必须知道自己从哪来。

**4. reduce 读-改-写。** 对每个 slug：读旧页面 + 本批新证据 → 一次 LLM 归并调用 →
写回完整页面。校验新增一条硬规则：归并结果里每条 key_point 的 `source_path`
**只能来自旧页面或本批证据的来源集合**——防止模型在归并时发明来源。

**5. 断点续跑。** 所有进度在 `state/` 的 JSON 里（临时文件 + `os.replace` 原子写入），
每批结束落盘。崩溃后重跑 `ingest`：done 的不再动，pending 的重新认领。
`--crash-after N` 可以现场演示。

## 核心设计：为什么 reduce 需要「读-改-写」

页面是**累积**的产物：第 1 批处理 engine.go 时页面写了引擎主循环；第 2 批处理
think.go 时，正确结果是「旧内容 + 新证据的归并」，而不是用 think.go 的摘要**覆盖**页面。
所以 reduce 必须先读旧页面，连同新证据一起交给 LLM 归并——这就是读-改-写。

**WeKnora 为什么给这一步上 slug 锁（`withSlugLock`）**：它的批次是并发的（asynq
多 worker），两个批次可能同时对同一 slug 做读-改-写。不加锁就是经典的 lost update：
A 读页面 → B 读页面 → A 写 → B 写（B 的写入把 A 的归并结果覆盖掉了）。
per-slug 锁把并发的读-改-写串行化。**教学版为什么不需要**：单进程顺序执行，
同一时刻只有一处在读-改-写，天然无竞争。这是「教学版砍掉并发治理」最典型的例子——
砍掉的不是正确性，而是并发场景本身。

## 代码解析（相对 s02 的增量）

| 新增/变化 | 说明 |
|---|---|
| `State` / `load_json` / `save_json` | 三个状态文件的读写；临时文件 + `os.replace` 原子落盘 |
| `file_hash` | sha256 前 16 位，manifest 幂等判定 |
| `MAP_PROMPT` | 产出 `{topics: [...]}`；带「已有 slug 优先复用」指令 |
| `REDUCE_PROMPT` | 读-改-写：`<old_page>` + `<evidence>` → 完整页面 JSON |
| `validate_map` / `validate_reduce` | 后者新增 source_path ∈ 允许来源集合的反幻觉校验 |
| `call_structured` | s01 的重试骨架，但失败**返回 None 而非退出**——失败要被隔离 |
| `cmd_ingest` 批循环 | 认领 ≤5 → map（失败记账）→ reduce → 标记 done → 落盘 |
| `MockLLM` 重写 | 确定性端到端：map 用正则从带行号内容提取真实函数定义行；slug 从目录导出（同目录文件自动归并演示 reduce）；reduce 机械合并 |
| `--mock-fail` / `--crash-after` | 演示失败隔离 / 崩溃恢复的注入开关 |
| `status` / `retry` 子命令 | 状态查看；dead-letter 人工重放 |

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版 |
|---|---|
| `cmd_ingest` 的批循环 | `ProcessWikiIngest`（`wiki_ingest.go`）：asynq 任务入口，同样是「认领 → map → reduce → 收尾」的循环 |
| `pending.json` + 内存认领 | `claimPendingList`（`wiki_ingest.go:667`）从 `task_pending_ops` 表原子认领（SQL `FOR UPDATE SKIP LOCKED` 风格），表带 `dedup_key` 去重、`fail_count` 计数 |
| `BATCH_SIZE = 5` | `wikiMaxDocsPerBatch = 5`（`wiki_ingest.go:117`）——教学版连数值都一致 |
| `MAX_FAIL_RETRIES = 2` + `failed` 状态 + `retry` 命令 | `wikiMaxFailRetries = 5`（:125）+ `requeueFailedOps`（:978）：未超限归还队列且保留 fail_count；超限丢弃并记 LastError 进 dead-letter |
| `map_one` → topics 列表 | `mapOneDocument`（`wiki_ingest_batch.go:1109`）→ `[]SlugUpdate` |
| `reduce_slug` 顺序读-改-写 | `reduceSlugUpdates`（`wiki_ingest_batch.go:1633`）包在 `withSlugLock`（`wiki_ingest.go:700`）里——并发批次必须串行化同一 slug 的读-改-写 |
| `state/` JSON + 原子替换 | PostgreSQL/SQLite 事务；崩溃恢复还有 `internal/container/reset_pending_tasks.go` 在启动时归还被中断进程认领走的 op |

**教学版砍掉了什么**：并发批次与 slug 锁（见上节）、asynq 队列与 worker 池治理、
per-model 在途配额与 429 退避、防抖触发（`wikiIngestDelay = 30s`，文档上传后攒批再触发
——s05 会用 git diff 替代这个触发机制）、引用分类 pass（`wiki_ingest_cite.go`）、
去重/目录规划/交叉链接/索引重建（这些是 s04 的内容）。

## 运行方法

```bash
cd C:\Desktop\Project\llm-wiki-learning\s03_map_reduce

# ① 崩溃恢复演示：map 完 1 个 op 后模拟崩溃（exit 99）
python code.py ingest --mock --path cmd/server --crash-after 1
python code.py status                    # op 仍是 pending —— 状态在磁盘上
python code.py ingest --mock --path cmd/server    # 重跑即从断点继续，完成全部

# ② 幂等演示：原样重跑，无事发生
python code.py ingest --mock --path cmd/server    # 新增/变更 0，队列空

# ③ 失败隔离演示：engine.go 永远失败，其余照常
python code.py ingest --mock --path internal/agent --limit 3 --mock-fail engine
python code.py status                    # 看到 failed op 与已生成的页面

# ④ dead-letter 重放：requeue 后正常处理，reduce 把新证据归并进已有页面（读-改-写）
python code.py retry
python code.py ingest --mock --path internal/agent --limit 3

# 真实 LLM：配置 .env 后去掉 --mock（建议保留 --limit 控制成本）
```

## 示例输出（失败隔离，步骤 ③）

```
[scan] 候选 24，其中新增/变更 21，已处理跳过 3
[scan] --limit 3：本次只入队前 3 个
[queue] 新入队 3，队列中 pending 3

[batch 1] 认领 3 个 op（批大小上限 5）
  [map] #4 internal/agent/act.go
  [map] #5 internal/agent/const.go
  [map] #6 internal/agent/engine.go
    [失败] map internal/agent/engine.go 第 1 次尝试：JSON 解析失败：...
    ...
    [requeue] #6 留在队列等待重试（attempts=1/2）
  [reduce] internal-agent ← 2 条更新
[batch 2] 认领 1 个 op ...
    [dead] #6 永久失败（attempts=2），可用 retry 命令人工重新入队

[完成] done=2 failed=1 页面=2（失败 op 用 `python code.py retry` 重新入队）
```

## 局限性

- map 失败重试会**重复付费**（op 重新认领后从头 map）——WeKnora 同样如此，
  这是「幂等靠重跑」的代价；
- 崩在 reduce 之后、标记 done 之前，重跑会把同一批证据再归并一次——
  靠归并 prompt 的去重指令兜底（WeKnora 靠事务把归并与标记绑在一起）；
- 页面还很粗糙：可能出现重复主题（两个 slug 讲同一件事）、没有互相链接、
  没有全局 index；
- Mock 的 slug 按目录导出，真实 LLM 才会给出语义化主题。

## 下一阶段（s04）

后处理流水线：页面去重合并（候选由代码筛、定夺由一次 LLM 调用）→
交叉链接注入（扫描全部页面标题/别名做文本匹配）→ 清理死链 →
重建 index.md（分组目录 + LLM 导读）。
对照 `deduplicateExtractedBatch` / `planBatchTaxonomy` / `injectCrossLinks` /
`cleanDeadLinks` / `rebuildIndexPage`。
