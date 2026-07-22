---
title: 模块 internal-agent-tools
type: module
status: draft
sources:
  - {path: internal/agent/tools/capabilities.go, lines: 21}
  - {path: internal/agent/tools/capabilities.go, lines: 41}
  - {path: internal/agent/tools/capabilities.go, lines: 83}
  - {path: internal/agent/tools/capabilities.go, lines: 101}
  - {path: internal/agent/tools/capabilities.go, lines: 107}
  - {path: internal/agent/tools/data_analysis.go, lines: 34}
  - {path: internal/agent/tools/data_analysis.go, lines: 38}
  - {path: internal/agent/tools/data_analysis.go, lines: 45}
  - {path: internal/agent/tools/data_analysis.go, lines: 76}
  - {path: internal/agent/tools/data_analysis.go, lines: 105}
  - {path: internal/agent/tools/data_schema.go, lines: 19}
  - {path: internal/agent/tools/data_schema.go, lines: 23}
  - {path: internal/agent/tools/data_schema.go, lines: 30}
  - {path: internal/agent/tools/data_schema.go, lines: 43}
  - {path: internal/agent/tools/database_query.go, lines: 100}
  - {path: internal/agent/tools/database_query.go, lines: 105}
  - {path: internal/agent/tools/database_query.go, lines: 112}
  - {path: internal/agent/tools/database_query.go, lines: 121}
  - {path: internal/agent/tools/database_query.go, lines: 257}
updated_at: 2026-07-22
---

# 模块 internal-agent-tools

## 概述

internal/agent/tools/capabilities.go 属于主题 internal-agent-tools，本文件贡献 5 条证据（mock 生成）。；internal/agent/tools/data_analysis.go 属于主题 internal-agent-tools，本文件贡献 5 条证据（mock 生成）。；internal/agent/tools/data_schema.go 属于主题 internal-agent-tools，本文件贡献 4 条证据（mock 生成）。；internal/agent/tools/database_query.go 属于主题 internal-agent-tools，本文件贡献 5 条证据（mock 生成）。

## 核心职责

- 提供 capabilities.go 中实现的能力（mock）
- 提供 data_analysis.go 中实现的能力（mock）
- 提供 data_schema.go 中实现的能力（mock）
- 提供 database_query.go 中实现的能力（mock）

## 关键实现

- 定义 KBCapability（第 21 行起）（来源：`internal/agent/tools/capabilities.go:21`）
- 定义 ToolRequirement（第 41 行起）（来源：`internal/agent/tools/capabilities.go:41`）
- 定义 hasCap（第 83 行起）（来源：`internal/agent/tools/capabilities.go:83`）
- 定义 KBFilter（第 101 行起）（来源：`internal/agent/tools/capabilities.go:101`）
- 定义 IsEmpty（第 107 行起）（来源：`internal/agent/tools/capabilities.go:107`）
- 定义 sqlSingleQuoteEscape（第 34 行起）（来源：`internal/agent/tools/data_analysis.go:34`）
- 定义 normalizeIdentifierForMatch（第 38 行起）（来源：`internal/agent/tools/data_analysis.go:38`）
- 定义 reconcileSQLColumnsWithSchema（第 45 行起）（来源：`internal/agent/tools/data_analysis.go:45`）
- 定义 buildMissingColumnSuggestion（第 76 行起）（来源：`internal/agent/tools/data_analysis.go:76`）
- 定义 DataAnalysisInput（第 105 行起）（来源：`internal/agent/tools/data_analysis.go:105`）
- 定义 DataSchemaInput（第 19 行起）（来源：`internal/agent/tools/data_schema.go:19`）
- 定义 DataSchemaTool（第 23 行起）（来源：`internal/agent/tools/data_schema.go:23`）
- 定义 NewDataSchemaTool（第 30 行起）（来源：`internal/agent/tools/data_schema.go:30`）
- 定义 Execute（第 43 行起）（来源：`internal/agent/tools/data_schema.go:43`）
- 定义 DatabaseQueryInput（第 100 行起）（来源：`internal/agent/tools/database_query.go:100`）
- 定义 DatabaseQueryTool（第 105 行起）（来源：`internal/agent/tools/database_query.go:105`）
- 定义 NewDatabaseQueryTool（第 112 行起）（来源：`internal/agent/tools/database_query.go:112`）
- 定义 Execute（第 121 行起）（来源：`internal/agent/tools/database_query.go:121`）
- 定义 validateAndSecureSQL（第 257 行起）（来源：`internal/agent/tools/database_query.go:257`）

## 调用关系

（无）

## 相关页面

- [模块 internal-agent](internal-agent.md)（共享来源目录）

## 证据与来源

本页面证据来自 4 个源文件：`internal/agent/tools/capabilities.go`、`internal/agent/tools/data_analysis.go`、`internal/agent/tools/data_schema.go`、`internal/agent/tools/database_query.go`。

## 未确认事项

- capabilities.go 的语义摘要需要真实 LLM 生成（mock 占位）
- data_analysis.go 的语义摘要需要真实 LLM 生成（mock 占位）
- data_schema.go 的语义摘要需要真实 LLM 生成（mock 占位）
- database_query.go 的语义摘要需要真实 LLM 生成（mock 占位）