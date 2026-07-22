---
name: analyze-go-project
description: 分析 Go 后端项目结构的方法论：入口、依赖注入、分层、常用 grep 模式。
---

# 分析 Go 后端项目

## 步骤

1. **确认模块与入口**：读 `go.mod`（模块名、Go 版本、关键依赖），`glob cmd/*/main.go`
   找入口。WeKnora 的入口是 `cmd/server/main.go`。
2. **找装配中心**：Go 大项目通常有一个 DI/装配点，读它等于拿到全系统地图。
   WeKnora 用 uber/dig：`internal/container/container.go` 的 `BuildContainer`——
   顺着 `Provide`/`Invoke` 调用能列出所有组件及其依赖。
3. **摸清分层**：`list_files internal/`。WeKnora 是
   `handler（HTTP/Gin）→ application/service（业务）→ application/repository（GORM）`，
   领域类型和接口契约在 `internal/types` 与 `internal/types/interfaces`。
4. **验证运行时分支**：搜环境变量开关（如 `os.Getenv("REDIS_ADDR")`），
   它们往往决定部署形态（WeKnora 的 Standard/Lite 双版本）。

## 常用 grep 模式

- 找接口实现：`func \(.+\) 方法名\(`
- 找注册点：`Provide|Invoke|Register`
- 找配置项：`os.Getenv|viper|cfg\.`
- 找路由：`GET\(|POST\(|Group\(`（Gin）

## 注意

- `*_test.go` 是理解行为的好材料，但不进 Wiki 正文；
- 结论必须带 `文件:行号`；从 container 的装配顺序推断的依赖关系要标注「推断」。
