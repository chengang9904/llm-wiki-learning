# s05：增量更新

## 本阶段目标

项目变了，Wiki 要跟上——但只重跑**受变更影响的部分**：

```
python code.py update
  ├─ 变更检测（自动降级）
  │    git 模式  git diff --name-status <last_ingest_commit>..HEAD
  │    哈希模式  全量扫描对比 manifest（首次运行 / 非 git 目录 / commit 失效）
  ├─ 新增/修改 → 入队 → 重跑 map-reduce（重新读源文件）
  ├─ 删除     → 页面还有其他来源：确定性剥除该来源的事实与来源行
  │             页面失去全部来源：标 status: deprecated（保留文件）
  └─ 成功后推进 last_ingest_commit + 写变更日志
```

## 上一阶段的问题

s03/s04 建好了 Wiki，但它是一张**快照**。项目一动，快照就开始说谎：
改过的文件页面里还是旧事实，删掉的文件页面还在引用。全量重跑能解决，
但 1000+ 文件的项目每次全量既慢又贵——而且 99% 的工作是重复的。

## 新增机制

**1. git 作为变更信号源。** state 记录 `last_ingest_commit`，
`git diff --name-status <commit>..HEAD` 直接给出 A/M/D 清单——找变更的成本从
O(项目) 降到 O(变更)。git 不可用（非仓库、commit 失效、git 未安装）就统一降级
到哈希模式：全量扫描对比 manifest，`processed_hash != hash` 即变更、
manifest 里有但磁盘上没有即删除。**git diff 给出的路径也要过同一套排除清单**
——变更信号不豁免安全红线。

**2. 必须重新读源文件。** 变更文件走的是和 s03 完全相同的 map（读文件 → 行号 →
截断 → 提取）。诱惑在于「旧摘要还在，让 LLM 拿旧摘要猜猜新版本」——绝不可以：
摘要是有损压缩，旧摘要 + 猜测 = 复印件的复印件。验证环节专门检查这一点：
修改文件后新增的函数必须以**真实的新行号**出现在页面里。

**3. 删除 ≠ 删页面。** 一个源文件被删除时，引用它的页面分两种命运：
- **还有其他来源** → 确定性剥除：frontmatter 里该来源的行、正文里引用该来源的
  事实条目直接删掉，并在「未确认事项」追加一条留痕（建议复核）。不调 LLM——
  这是结构完全可预知的清理，流水线的活；
- **失去全部来源** → 标 `status: deprecated`，文件保留。页面对应的知识可能还有效
  （代码挪走了而非消失），直接删除会丢线索——留给人或 s12 的 Fixer Agent 判断。

**4. 部分更新不推进 commit。** 带 `--path`/`--limit` 时只处理了子集，
若照常把 `last_ingest_commit` 推到 HEAD，下次 update 会以为「HEAD 之前都同步过了」，
漏掉这次没处理的变更——所以部分更新不推进标记。一个典型的「状态标记必须和
实际完成的工作对齐」的坑。

**5. reduce 的新规则：同文件新证据覆盖旧事实。** 修改过的文件重新 map 后，
归并 prompt 明确「同一来源文件的旧事实与新证据冲突时以新证据为准」；
Mock 同样实现了这一点（旧页面里来自本批文件的事实直接丢弃，用新证据替换）。

## 代码解析（相对 s03 的增量）

| 新增/变化 | 说明 |
|---|---|
| `run_git` / `detect_changes` | git diff 主路径 + 哈希 fallback，所有 git 失败走同一条降级路 |
| `passes_filters` | 目录/敏感/扩展名/模式过滤抽成独立函数，git 路径也走它 |
| `handle_deleted_source` | 剥除来源 / 标 deprecated 两分支；同时清 manifest 与 pending 队列 |
| `_append_uncertainty` | 往「未确认事项」小节追加留痕 |
| `_record_commit` | 推进 last_ingest_commit；`--path`/`--limit` 时拒绝推进 |
| `REDUCE_PROMPT` 新规则 | 同文件新证据覆盖旧事实（Mock 同步实现） |
| `process_queue` | s03 的批处理循环原样抽成函数（幂等/失败隔离/断点续跑都在） |
| 环境变量 `WIKI_WORKSPACE` | workspace 可重定向，便于隔离演示与测试 |

## 对照 WeKnora 真实实现

| 教学版 | WeKnora 生产版 |
|---|---|
| 手动 `update` 命令 + git diff | 事件驱动：文档上传/删除直接写 `task_pending_ops`（数据库触发），带**防抖** `wikiIngestDelay = 30s`（`wiki_ingest.go:97`）——30 秒内的连续上传攒成一批。教学版两次 update 之间的全部提交天然就是一批，防抖不需要 |
| 变更走 s03 的 op 队列 | 同一套 `task_pending_ops`（dedup_key 去重、fail_count 重试），s03 已对照过 |
| `handle_deleted_source` | 删除侧有两道防线：`markKnowledgeDeletedForWiki`（`knowledge_delete.go:337`）写短 TTL **墓碑**，防止在途 ingest 任务给刚删除的文档建页（教学版单进程无在途任务，不需要）；`sanitizeDeadSummaryLinks`（`wiki_ingest.go:1302`）清理指向已删来源的摘要链接 |
| 标 deprecated 保留页面 | WeKnora 页面 `Status` 有 archived 一档（`types/wiki_page.go:140`），同样是「不可见但不销毁」 |
| `last_ingest_commit` | WeKnora 没有这个概念——它的「进度标记」就是队列本身（op 处理完即消失）。educational git 标记是对「拉模式」增量的适配 |

**教学版砍掉了什么**：事件驱动与防抖、墓碑竞态防御、未提交工作区变更的感知
（git diff 只看提交历史——见局限性）、按 KB 分片的队列 scope。

## 运行方法

演示不动 WeKnora——用环境变量把源目录和 workspace 都指到一个临时 git 仓库：

```powershell
cd C:\Desktop\Project\llm-wiki-learning\s05_incremental_update

# 0) 准备演示仓库（3 个 go 文件，两个包），git init + commit
#    pkg/alpha/{one.go,two.go}  pkg/beta/solo.go
$env:SOURCE_ROOT = "...\demo-src"; $env:WIKI_WORKSPACE = "...\demo-src-workspace"

# 1) 首次 update：无 last_ingest_commit → 哈希模式全量，之后记录 HEAD
python code.py update --mock

# 2) 制造变更并提交：改 one.go（加函数）、增 three.go、删 two.go、删 solo.go
# 3) 第二次 update：git diff 模式，2 A/M + 2 D
python code.py update --mock
python code.py status

# 4) 第三次 update：无变更，秒完成
python code.py update --mock
```

对真实 WeKnora workspace 使用时直接 `python code.py update --mock`（或真实 LLM），
建议配 `--limit` 控制单次成本；首次运行会走哈希模式并把所有未处理文件视为新增。

## 示例输出（第二次 update）

```
[detect] 模式：git diff b9c241f9..HEAD
[detect] 新增/修改 2，删除 2
[delete] 处理 2 个已删除的源文件
  [剥除] pkg-alpha：移除来源 pkg/alpha/two.go（2 条事实），剩余 1 个来源
  [废弃] pkg-beta：全部来源已删除，标记 status: deprecated（文件保留）
[queue] 入队 2 个 op（重新读源文件做 map，绝不只凭旧摘要）
  [map] #4 pkg/alpha/one.go
  [map] #5 pkg/alpha/three.go
  [reduce] pkg-alpha ← 2 条更新
[commit] last_ingest_commit → dd8a133b
```

更新后的 pkg-alpha 页面：`Farewell（第 14 行起）` 出现（新函数、真实新行号——
证明重新读了源文件）、two.go 的事实消失；pkg-beta 页面 `status: deprecated`，
未确认事项里留痕「来源 pkg/beta/solo.go 已于 … 删除」。

## 局限性

- git diff 只看**提交历史**：未提交的工作区改动不可见（哈希模式可见，但需要全量扫描）；
- 剥除来源是行级文本操作，若页面被人工改过格式（引用写法变了）可能剥不干净——
  留痕 + s06 lint 的「缺来源」检查兜底；
- deprecated 页面仍留在链接图里，指向它的链接不算死链——是否降权/摘除
  由 s04 重跑与 s06 lint 处理；
- 同一文件反复改动会反复全文重 map，没有 chunk 级的细粒度增量（WeKnora 有 chunk 层）。

## 下一阶段（s06）

Wiki Lint：纯确定性检查（不调 LLM）——断链、指向不存在源文件的引用、缺来源页面、
长期 draft、标题冲突、孤立页面、缺反向链接、index 未收录。输出结构化报告写入
`state/issues.json`，**只报告不修改**——修复是 s12 Fixer Agent 的工作（判断类任务）。
对照 `wiki_lint.go`、`wiki_flag_issue` 的 issue 数据结构。
