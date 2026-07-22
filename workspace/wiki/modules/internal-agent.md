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
updated_at: 2026-07-22
---

# 模块 internal-agent

## 概述

internal/agent/engine.go 属于主题 internal-agent，本文件贡献 5 条证据（mock 生成）。；internal/agent/finalize.go 属于主题 internal-agent，本文件贡献 4 条证据（mock 生成）。；internal/agent/image_requirement.go 属于主题 internal-agent，本文件贡献 2 条证据（mock 生成）。

## 核心职责

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

## 调用关系

（无）

## 相关页面

（暂无——交叉链接由 s04 后处理注入）

## 证据与来源

本页面证据来自 5 个源文件：`internal/agent/act.go`、`internal/agent/const.go`、`internal/agent/engine.go`、`internal/agent/finalize.go`、`internal/agent/image_requirement.go`。

## 未确认事项

- engine.go 的语义摘要需要真实 LLM 生成（mock 占位）
- finalize.go 的语义摘要需要真实 LLM 生成（mock 占位）
- image_requirement.go 的语义摘要需要真实 LLM 生成（mock 占位）
