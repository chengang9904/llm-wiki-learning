---
title: 模块 internal-agent
type: module
status: draft
sources:
  - {path: internal/agent/act.go, lines: 31}
  - {path: internal/agent/act.go, lines: 45}
  - {path: internal/agent/act.go, lines: 58}
  - {path: internal/agent/act.go, lines: 107}
  - {path: internal/agent/act.go, lines: 142}
  - {path: internal/agent/const.go, lines: 54}
  - {path: internal/agent/const.go, lines: 68}
  - {path: internal/agent/const.go, lines: 76}
  - {path: internal/agent/engine.go, lines: 36}
  - {path: internal/agent/engine.go, lines: 60}
  - {path: internal/agent/engine.go, lines: 63}
  - {path: internal/agent/engine.go, lines: 105}
  - {path: internal/agent/engine.go, lines: 110}
  - {path: internal/agent/finalize.go, lines: 17}
  - {path: internal/agent/finalize.go, lines: 27}
  - {path: internal/agent/finalize.go, lines: 160}
  - {path: internal/agent/finalize.go, lines: 181}
  - {path: internal/agent/image_requirement.go, lines: 24}
  - {path: internal/agent/image_requirement.go, lines: 35}
  - {path: internal/agent/engine.go, lines: 349-433}
  - {path: internal/agent/engine.go, lines: 435-448}
  - {path: internal/agent/engine.go, lines: 450-535}
updated_at: 2026-07-22
---

# 模块 internal-agent

## 概述

engine.go 实现 WeKnora 的 ReAct Agent 引擎，是「模型即决策者」范式的核心。它以 executeLoop 为主循环，每一轮调用 runReActIteration 执行 think → act → observe，直到模型给出最终答案或达到 MaxIterations 上限。循环走向由 iterOutcome 哨兵值控制。 internal/agent/engine.go 属于主题 internal-agent，本文件贡献 5 条证据（mock 生成）。；internal/agent/finalize.go 属于主题 internal-agent，本文件贡献 4 条证据（mock 生成）。；internal/agent/image_requirement.go 属于主题 internal-agent，本文件贡献 2 条证据（mock 生成）。

## 核心职责

- 驱动 ReAct 主循环并在每轮之间维护 Agent 状态
- 在达到最大迭代数时兜底收尾，保证一定有输出
- 把工具调用结果拼回对话历史供下一轮推理
- 提供 engine.go 中实现的能力（mock）
- 提供 finalize.go 中实现的能力（mock）
- 提供 image_requirement.go 中实现的能力（mock）

## 关键实现

- 定义 truncateForLangfuse（第 31 行起）（来源：`internal/agent/act.go:31`）
- 定义 argKeys（第 45 行起）（来源：`internal/agent/act.go:45`）
- 定义 finishToolSpan（第 58 行起）（来源：`internal/agent/act.go:58`）
- 定义 dataKeys（第 107 行起）（来源：`internal/agent/act.go:107`）
- 定义 formatToolHint（第 142 行起）（来源：`internal/agent/act.go:142`）
- 定义 isTransientError（第 54 行起）（来源：`internal/agent/const.go:54`）
- 定义 getLLMCallTimeout（第 68 行起）（来源：`internal/agent/const.go:68`）
- 定义 generateEventID（第 76 行起）（来源：`internal/agent/const.go:76`）
- 定义 AgentEngine（第 36 行起）（来源：`internal/agent/engine.go:36`）
- 定义 ImageDescriberFunc（第 60 行起）（来源：`internal/agent/engine.go:60`）
- 定义 NewAgentEngine（第 63 行起）（来源：`internal/agent/engine.go:63`）
- 定义 SetPinnedMentions（第 105 行起）（来源：`internal/agent/engine.go:105`）
- 定义 systemPromptOptions（第 110 行起）（来源：`internal/agent/engine.go:110`）
- 定义 finalAnswerImageRequirement（第 17 行起）（来源：`internal/agent/finalize.go:17`）
- 定义 streamFinalAnswerToEventBus（第 27 行起）（来源：`internal/agent/finalize.go:27`）
- 定义 handleMaxIterations（第 160 行起）（来源：`internal/agent/finalize.go:160`）
- 定义 emitCompletionEvent（第 181 行起）（来源：`internal/agent/finalize.go:181`）
- 定义 stepContainsMarkdownImage（第 24 行起）（来源：`internal/agent/image_requirement.go:24`）
- 定义 appendAgentRetrievedImageRequirement（第 35 行起）（来源：`internal/agent/image_requirement.go:35`）
- executeLoop 主循环，以 state.CurrentRound < MaxIterations 为边界（来源：`internal/agent/engine.go:349-433`）
- iterOutcome 哨兵值决定循环 continue/break，而非裸返回值（来源：`internal/agent/engine.go:435-448`）
- runReActIteration 单轮 think → analyze → act → observe（来源：`internal/agent/engine.go:450-535`）

## 调用关系

（无）

## 相关页面

- [模块 internal-agent-tools](internal-agent-tools.md)（共享来源目录）

## 证据与来源

本页面证据来自 5 个源文件：`internal/agent/act.go`、`internal/agent/const.go`、`internal/agent/engine.go`、`internal/agent/finalize.go`、`internal/agent/image_requirement.go`。

## 未确认事项

- 合并页面的语义归纳需要真实 LLM（mock 机械合并）