---
name: analyze-grpc-service
description: 分析 gRPC 服务契约与两侧实现的方法论：proto → 服务端 → 客户端。
---

# 分析 gRPC 服务

## 步骤

1. **从 proto 开始**：契约是唯一事实。`glob **/*.proto` 定位；
   WeKnora：`docreader/proto/docreader.proto`。读 service 定义与 message 结构。
2. **服务端实现**：按 proto 的 service 名搜实现
   （Python：`add_.*Servicer_to_server`；Go：`Register.*Server`）。
   docreader 的服务端在 `docreader/main.py` + `docreader/server/`。
3. **客户端**：搜 `New<Service>Client`。WeKnora Go 侧：
   `internal/infrastructure/docparser/grpc_parser.go`（`proto.NewDocReaderClient`），
   由 `internal/container/container.go` 的 `initDocReaderClient` 注册进 DI。
4. **生成物与再生成**：`*.pb.go` / `*_pb2.py` 是生成码（工具已排除，不要分析），
   再生成命令看 `docreader/Makefile`（`make proto`）。
5. **连接配置**：搜地址环境变量（`DOCREADER_ADDR`，默认 :50051）与超时/重试设置。

## 结论要求

调用链写成：调用方法（Go 文件:行号）→ proto RPC 名 → 服务端实现（Python 文件）。
