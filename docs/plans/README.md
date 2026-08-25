# Plan Index

| Topic | Status | Scope | Summary | Last verified |
|---|---|---|---|---|
| [Intake Required Material Slots](2026-08-24_intake-required-material-slots.md) | Implemented | explicit paste-target selection, immediate payment recognition refresh, category-first metadata, direct drop/picker/paste upload, MCP recipe, docs/tests | 已完成材料空槽交互与识别刷新，并通过现有 MCP 工具发布缺件映射、版本前置条件和写后重读流程 | 2026-08-26 |
| [GitHub Publication Hardening](2026-08-24_github-publication-hardening.md) | Implemented | repository boundary, secret/financial redaction, anonymous DOCX template, private GitHub publication | 已以批准文件白名单完成私有 GitHub 首次发布；本地与远端提交一致，秘密、PII、路径、二进制和敏感目录门禁均通过 | 2026-08-24 |
| [Reimbursement Batch Return and Merge](2026-08-24_batch-return-and-merge.md) | Implemented | universal history return-to-edit, same-project drafts merged into the current target, unified export, unchanged Agent tools, docs/tests | 已完成所有历史报销包退回编辑、同项目处理中包安全并入当前目标、恢复快照与统一导出闭环，Agent 工具清单未扩展 | 2026-08-24 |
| [Document Intake Association and Preview](2026-08-24_document-intake-association-preview.md) | Implemented | intake frontend Feature, additive association/preview API, drag-and-drop, docked image/PDF reader, docs/tests | 已修复旧后台路由表导致的附件关联 404，并完成图片/PDF 右侧内嵌阅读器、版本能力保护与正式服务实机验收 | 2026-08-24 |
| [Windows Local Agent MCP](2026-08-23_windows-local-agent-mcp.md) | Implemented | STDIO MCP, API consistency contracts, Windows Codex/Hermes registration, isolated validation | 已完成受控 Agent 接入、双客户端实机验收、一致恢复演练和生产切换；默认 15 项工具、写审批、版本/幂等/核对与文件授权边界均已验证 | 2026-08-24 |
| [DeepSeek Recognition Provider Migration](2026-08-23_deepseek-recognition.md) | Implemented | recognition backend Feature, PDF preprocessing, settings UI, environment/docs/tests | 已将不可用的 OpenAI 识别路径替换为唯一 DeepSeek 提供方，并增加 PDF 分页转图和最近一次可用状态；全量测试、语法检查和真实浏览器烟测通过 | 2026-08-23 |
| [Lightweight Feature Boundaries and Quotation Pilot](2026-08-12_lightweight-feature-boundaries.md) | Implemented | `AGENTS.md`, `docs/`, frontend shared modules, quotation frontend/backend routes, architecture tests | 已在不引入前端框架和不改变业务行为的前提下建立首个垂直 Feature 切片；全量测试、语法/编译和真实浏览器烟测通过 | 2026-08-12 |

状态使用 `Draft`、`Active`、`Implemented`、`Superseded`、`Cancelled`。`Implemented` 只表示 owning Plan 已完成，不替代当前代码和测试这一事实源。
