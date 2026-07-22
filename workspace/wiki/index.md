# Wiki 索引

> 更新于 2026-07-22，共 4 个页面。本索引由后处理流水线自动重建，请勿手工编辑。

本 Wiki 由确定性流水线从 WeKnora 源码生成，覆盖后端模块与启动流程。建议从 architecture 分组读起，再按模块页面下钻；每条关键事实都带行号来源。（mock 导读）

## architecture

- [Agent 架构总览（示例）](architecture/agent-overview.md) — 这是一个模拟「早前由人工撰写」的页面，用来演示后处理的三个机制。

## module

- [模块 cmd-server](modules/cmd-server.md) — cmd/server/bootstrap.go 属于主题 cmd-server，本文件贡献 2 条证据（mock 生成）。
- [模块 internal-agent](modules/internal-agent.md) — engine.go 实现 WeKnora 的 ReAct Agent 引擎，是「模型即决策者」范式的核心。
- [模块 internal-agent-tools](modules/internal-agent-tools.md) — internal/agent/tools/capabilities.go 属于主题 internal-agent-tools，本文件贡献 5 条证据（mock 生成）。
