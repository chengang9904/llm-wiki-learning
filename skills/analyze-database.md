---
name: analyze-database
description: 分析数据库模型与迁移的方法论：GORM 模型、多后端迁移集、租户隔离字段。
---

# 分析数据库模型

## 步骤

1. **模型定义**：WeKnora 的 GORM 模型在 `internal/types/`（struct 带 `gorm:` tag）。
   `grep "gorm:" internal/types` 列出全部持久化字段；注意 `tenant_id` /
   `workspace` 类字段——多租户隔离的物理基础。
2. **迁移集（注意有多套！）**：`migrations/versioned/`（Postgres，编号 .up/.down 对）、
   `migrations/sqlite/000000_init.*`（SQLite 单文件合并 schema）、另有
   `migrations/paradedb/`、`migrations/mysql/`。**一个 schema 变更通常要同时改
   Postgres 和 SQLite 两套**——分析表结构以迁移文件为准，模型 tag 为辅。
3. **自动迁移**：启动时自动跑（`AUTO_MIGRATE=false` 关闭）；恢复脏状态用
   `make migrate-force`。
4. **表 ↔ 仓储**：`internal/application/repository/` 一个领域一个 repo；
   检索类另有 per-backend 目录 `repository/retriever/<engine>/`。

## 结论要求

- 表结构引用迁移文件（哪套后端的哪个版本）；
- 指出跨后端差异（如 SQLite 无 pgvector，向量检索走别的引擎）；
- 租户隔离字段必须点名。
