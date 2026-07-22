---
title: Agent ReAct 引擎
type: module
status: draft
sources:
  - {path: internal/agent/engine.go, lines: 349-433}
  - {path: internal/agent/engine.go, lines: 435-448}
  - {path: internal/agent/engine.go, lines: 450-535}
updated_at: 2026-07-22
---

# Agent ReAct 引擎

## 概述

engine.go 实现 WeKnora 的 ReAct Agent 引擎，是「模型即决策者」范式的核心。它以 executeLoop 为主循环，每一轮调用 runReActIteration 执行 think → act → observe，直到模型给出最终答案或达到 MaxIterations 上限。循环走向由 iterOutcome 哨兵值控制。

## 核心职责

- 驱动 ReAct 主循环并在每轮之间维护 Agent 状态
- 在达到最大迭代数时兜底收尾，保证一定有输出
- 把工具调用结果拼回对话历史供下一轮推理

## 关键实现

- executeLoop 主循环，以 state.CurrentRound < MaxIterations 为边界（来源：`internal/agent/engine.go:349-433`）
- iterOutcome 哨兵值决定循环 continue/break，而非裸返回值（来源：`internal/agent/engine.go:435-448`）
- runReActIteration 单轮 think → analyze → act → observe（来源：`internal/agent/engine.go:450-535`）

## 调用关系

- 调用 internal/agent/tools 的工具注册表执行工具
- 使用 internal/types 的领域类型
- 推测被 chat 服务层在 agent 模式下调用（需看 service 层证实）

## 相关页面

（暂无——交叉链接由 s04 后处理注入）

## 证据与来源

本页面全部关键事实来自 `internal/agent/engine.go`，各要点的行号引用见「关键实现」一节。

## 未确认事项

- MaxIterations 的默认值来自何处（配置文件还是常量）未在本文件确认
- handleMaxIterations 的兜底输出策略细节需要展开阅读
