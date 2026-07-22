---
name: analyze-vue-project
description: 分析 Vue 3 前端项目的方法论：构建配置、状态管理、路由、API 层。
---

# 分析 Vue 3 前端项目

## 工具注意事项（先读这条）

遍历类工具（list_files/grep/glob）的目录排除清单包含 `frontend/`（为了挡住
node_modules 级别的噪音），但 **read_file 不受目录排除影响**——分析前端时
直接 read_file 具体文件（如 `frontend/package.json`）。

## 步骤

1. **依赖与脚本**：read_file `frontend/package.json`——UI 库（WeKnora 用 TDesign）、
   状态管理（Pinia）、构建（Vite）、测试命令一目了然。
2. **构建与代理**：`frontend/vite.config.ts`——dev server 端口、API 代理目标
   （WeKnora 代理到 :8080 的 Go 后端）。
3. **入口与路由**：`frontend/src/main.ts` → `frontend/src/router/`——页面清单
   就是功能清单。
4. **API 层**：找 `src/api/` 或 axios 封装——前端调用了后端哪些接口，
   与后端 handler 对照能画出前后端契约。
5. **状态**：`src/stores/`（Pinia）——全局状态的形状反映核心业务对象。

## 结论要求

前端页面 ↔ 后端路由的对应关系要给出两侧文件引用；i18n 文案（vue-i18n）
可作为功能命名的辅助证据。
