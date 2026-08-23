# Lightweight Feature Boundaries and Quotation Pilot

Status: Implemented

## Objective

在保持现有 Flask、原生 HTML/CSS/JavaScript、API URL 和用户行为不变的前提下，建立可供后续 Feature 复用的最小工程边界，并以“报价测算”完成第一个前后端垂直切片。

完成后：

- 开发者能从 Plan 索引定位当前工作，并从模块 README 了解责任和依赖。
- `web/app.js` 不再拥有报价测算实现，只通过一个公共入口组合该 Feature。
- DOM、HTTP、反馈 UI、表单读取和应用状态拥有明确共享模块。
- 报价计算与导出路由由独立 Blueprint 暴露，现有 `/api/quotations/*` 契约保持不变。

## Out of scope

- 不引入 Vue、Vite、TypeScript、Storybook 或 npm 依赖。
- 不重做视觉样式，不修改报价算法、Word 模板或数据库 Schema。
- 不一次性迁移录入、报销池、工作区、历史或设置页面。
- 不在未选择 Git 或 Plastic 的情况下初始化版本管理。

## Dependency contract

- 前端：`app -> features -> shared`。
- 后端：Feature Blueprint 依赖领域/导出模块；领域/导出模块不反向依赖 Blueprint。
- 报价 Feature 只能通过 `renderQuotation(pageVersion)` 被应用 Shell 调用。

## Milestones and gates

### 1. Establish governance and shared frontend primitives

新增根工程约束、文档入口、Plan 索引、架构说明，以及 `core/state`、DOM、HTTP、反馈 UI 和表单模块。

Gate：现有页面继续从同一个 state 实例和相同 HTTP/DOM 行为运行；所有模块通过 Node 语法检查。

### 2. Extract the quotation vertical slice

把报价渲染、交互和导出编排移动到 `web/features/quotation/`，把报价 HTTP 路由移动到 `invoice_assistant/features/quotation/`。

Gate：`web/app.js` 只导入 `renderQuotation`；两个原 API URL、响应、Word 下载和错误码保持原样；现有报价专项测试通过。

### 3. Lock the boundary and validate

增加静态资产、模块入口和 Blueprint 所有权测试，运行全量 pytest、全部前端 JS 语法检查，并在真实浏览器中执行仪表盘与报价默认测算烟测。

Gate：pytest 零失败，JS 零语法错误，浏览器 Console 无错误，默认合成报价结果保持与迁移前一致。

## Validation evidence

- Baseline 2026-08-12: `35 passed` in `4.62s`.
- Baseline 2026-08-12: Node `v24.14.0`; legacy `web/app.js` passed `node --check`.
- Final 2026-08-12: `web/app.js` reduced from 1266 to 759 lines; quotation behavior moved behind `web/features/quotation/index.js::renderQuotation`.
- Final 2026-08-12: quotation HTTP ownership moved to `quotation_api` while both `/api/quotations/*` URLs and existing calculation/export modules remained unchanged.
- Final 2026-08-12: `38 passed` in `4.43s`, including three new architecture-contract tests.
- Final 2026-08-12: all 7 frontend JavaScript files passed `node --check`; all 15 package Python files passed `py_compile`.
- Final 2026-08-12 browser smoke: dashboard and quotation loaded through native ES Modules; the default synthetic total remained unchanged; description toggle, add/remove item, and export modal worked; browser Console contained zero errors.
