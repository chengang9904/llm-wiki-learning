---
name: analyze-python-service
description: 分析 Python 服务的方法论：依赖清单、入口、插件注册表（以 docreader 为例）。
---

# 分析 Python 服务

## 步骤

1. **依赖与元数据**：read_file `docreader/pyproject.toml`（uv 管理，锁在 uv.lock）——
   解析库（PDF/Word/OCR）决定服务能力边界。
2. **入口**：`docreader/main.py`——服务如何启动、监听什么（gRPC :50051）、
   支持哪些 transport（`DOCREADER_TRANSPORT`）。
3. **插件注册表**：`docreader/parser/registry.py`——一个格式一个 parser 文件的
   注册模式；列出注册表就得到支持格式清单。
4. **与主服务的边界**：Go 侧通过 gRPC 调用（见 analyze-grpc-service 技能）；
   proto 契约在 `docreader/proto/docreader.proto`。

## 常用模式

- 找注册：`register|REGISTRY|entry_points`
- 找配置：`os.environ|getenv`
- 找 gRPC 服务实现：`class .*Servicer|add_.*Servicer_to_server`

## 注意

`__pycache__`、`.venv` 已被工具排除；测试在 `docreader/` 下用 pytest。
