# InvoicesHandler Architecture

## 目标

项目继续使用 Flask 提供本地服务和静态页面。现阶段采用浏览器原生 ES Modules 建立 Feature 边界，不引入 Vue、Vite、TypeScript 或 Node 运行时依赖。

## 前端依赖方向

```text
app -> features -> shared
```

- `web/app.js`：应用 Shell、导航、Feature 组合，以及尚未迁移的旧页面。
- `web/features/<feature>/`：一个业务能力的 UI、交互编排和 Feature 私有状态。
- `web/shared/`：DOM、HTTP、通用反馈和表单读取等不含具体业务语义的工具。
- `web/core/`：应用级状态。Feature 可以读取自己的状态片段，但不得通过它操作其他 Feature 的内部行为。

`shared` 不得依赖 `features` 或 `app`。Feature 之间不得深层互相导入；跨 Feature 协作由应用 Shell 或稳定公共入口编排。

## 后端边界

- `invoice_assistant/features/<feature>/routes.py`：该 Feature 的 HTTP Blueprint，只负责协议适配。
- 领域计算、导出、存储和持久化模块不得依赖具体 Blueprint。
- 公共 JSON 请求解析等传输工具放在 `invoice_assistant/http.py`。
- 现有 API URL 和 JSON 契约默认保持兼容。

本地 Agent 是独立协议边界：

- `invoice_assistant/features/agent_mcp/` 只通过固定 loopback HTTP API 调用应用，不得导入数据库、存储、识别、导出或其他 Feature 的私有实现。
- Agent 专用 HTTP Blueprint 只暴露经过裁剪的持久 operation 状态；幂等、核对、分页、文件暂存和导出状态机仍是可脱离 MCP/Flask 传输层测试的领域模块。
- STDIO stdout 只承载 MCP 协议；运行诊断进入 stderr。MCP 不拥有网页服务生命周期，也不自动启动服务。
- 数据库建库/升级只由 `python -m invoice_assistant.migrate` 在数据目录互斥锁下执行；`create_app()` 与 `run.py` 启动路径只读校验 schema，不能隐式迁移。

## 共享与复用

代码不会仅因“未来可能复用”进入 `shared`。只有无业务语义、边界稳定，或已经出现第二个真实使用者的能力才提升为共享模块。业务组件即使较大，也优先保留在 owning Feature 中。

## Harness 与验证层级

- 纯领域函数：单元测试。
- Flask 路由、数据库和文件闭环：pytest。
- 原生前端模块：Node 语法检查和静态模块契约测试。
- 用户可见流程：真实浏览器烟测。

当复杂可复用组件和状态矩阵明显增加时，再由独立 Plan 评估 Storybook/Vitest；当 DOM 状态编排成本持续上升时，再评估 Vue/TypeScript/Vite。
