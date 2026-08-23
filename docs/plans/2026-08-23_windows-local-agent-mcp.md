# 发票助手本地 Agent MCP 接入（Windows 第一阶段）

Status: Implemented

Last verified: 2026-08-24

## 决策摘要

- 第一阶段只交付并声明支持 Windows 本机；Apple Silicon macOS 另建 owning Plan，在 Mac 实机上实现和验收。
- 采用 STDIO MCP。MCP 只调用发票助手公开 HTTP 接口，不直接读写 SQLite、持久目录或 Blueprint 私有实现。
- Windows 第一阶段只支持 `http://127.0.0.1:8765`；不提供半成品的 host/port 配置能力。
- 新增从脚本位置推导项目根目录的稳定启动器；客户端配置不得写死开发机盘符或依赖调用方当前目录。
- 首版文件工具每次只接收一个文件、单文件不超过 20 MiB；不提供批量 Agent 导入。
- allowed roots 默认为空。未显式授权目录时不暴露文件工具；绝不默认授权项目目录或整个用户主目录。
- 所有写工具都要求 `operation_id`，由后端持久化幂等结果；所有实体写入同时使用版本前置条件，不能覆盖调用方尚未读取的新状态。
- 写工具只有在后端幂等、版本和核对合同通过后才注册到 Codex/Hermes；不能用进程内锁冒充跨客户端一致性。
- `install.ps1` 继续只负责应用和依赖安装，不自动修改 Agent 配置；用户通过独立脚本显式注册或移除 MCP。

## 当前基线与前置条件

- 当前代码、测试、根 `README.md`、`docs/architecture.md` 和匹配的 owning Plan 是事实源；本文件在获准放入仓库前只是候选计划。
- `docs/plans/2026-08-23_deepseek-recognition.md` 当前为 `Implemented`，Plan 索引一致；其中记录的 44 项 pytest、全量 JavaScript 语法检查、浏览器烟测和一次真实 DeepSeek 识别是本计划的已知基线，不在本计划中重复拥有识别迁移。
- 本机已观测到 Codex CLI `0.149.0` 和 Hermes Agent `0.20.1`。实施开始后的源码、随附参考和只读能力探测确认：Hermes `trust: untrusted` 对每一次未标记 `readOnlyHint=true` 的调用执行批准/拒绝分流，且不允许永久放行；这比候选 Plan 原先假定的“一次性 server trust”更严格。owning Plan 据动态证据修订为逐次写调用审批，仍不能替代 M4/M5 的隔离 discovery 与真实审批验收。
- Hermes `0.20.1` 还存在 `tools.include: []` 被解释为“未设置”并可能注册全部工具的 fail-open 行为；注册器必须拒绝空 manifest。当前 15/17 项清单始终非空，但测试仍需锁定这一失败边界。
- MCP 不读取 `DEEPSEEK_API_KEY`，也不直接调用 DeepSeek。导入和支付记录识别始终经发票助手 API，继续保持“识别失败仍保存受管副本并创建或保留可手工处理记录”的产品行为。
- 开始实现时，将本计划复制为 `docs/plans/2026-08-23_windows-local-agent-mcp.md`，状态改为 `Active` 并更新 `docs/plans/README.md`；实现前重新执行全量基线并记录实际结果。
- 目标客户端必须通过能力探测：Codex 要支持 STDIO、工具白名单、写审批、工作目录和超时；Hermes 要支持 `trust: untrusted`、工具白名单、禁用 resources/prompts、关闭并行工具调用和超时。能力不足时注册必须失败并给出升级指引，不能降级为无审批写工具。

## 目标

在不开放远程访问、不自动启动业务服务、不扩大文件权限的前提下，为 Windows 本机 Codex 和 Hermes 提供受控的 MCP 接口，使用户能够完成发票草稿核对、确认、重复处置、报销包创建和归档导出，并且任何超时重试、并发写入和文件读取都有可验证的安全边界。

## 范围

### In scope

- 平台中立的 MCP 协议适配、工具 Schema、稳定 DTO、错误映射和本地 HTTP 客户端。
- Windows 专用启动器，以及 Codex/Hermes 的显式注册、更新、移除、备份和失败回滚。
- 本计划明确列出的 API 合同迁移：持久幂等、实体版本、核对令牌和可分页列表。
- MCP 文件读取的显式授权、路径防逃逸、单次有界读取和日志脱敏。
- 隔离自动化测试、真实 STDIO 测试、浏览器验证，以及 Windows Codex/Hermes 实机验收。
- owning Plan、Plan 索引、Feature README、根 README、卸载提示和验证证据。

### Out of scope

- HTTP 端口可配置化、远程主机、局域网或公网 MCP，以及新的认证系统。
- MCP 自动安装、启动、停止或重启发票助手服务。
- `install.ps1` 自动注册 Agent，或在没有用户显式操作时修改 Codex/Hermes 配置。
- 多文件 Agent 导入、MCP resources/prompts、MCP task/streaming 工作流。
- 删除发票或附件、删除/退回/重开历史、标记已报销、修改全局设置、打开本地目录或文件，以及报价单能力。
- 前端框架、打包器、包管理器或全局状态库迁移。
- macOS 安装、LaunchAgent、Finder 集成和 macOS 客户端配置。

## 架构与运行边界

### 目录职责

- 新建 `invoice_assistant/features/agent_mcp/`：
  - `server.py`：MCP 工具注册和 STDIO 入口。
  - `contracts.py`：唯一工具清单、输入/输出 Schema 和 ToolAnnotations。
  - `client.py`：固定 loopback HTTP 客户端、超时和错误映射。
  - `dto.py`：字段白名单、金额/时间转换和输出截断。
  - `routes.py`：仅承载本计划新增的操作状态 HTTP 适配；MCP 运行时不得导入它。
  - `registration.py`：Codex TOML、Hermes YAML 的格式保留式读写和注册状态校验；MCP 运行时不得导入它。
  - `README.md`：当前责任、公共入口、依赖和安全边界。
- 新建 `invoice_assistant/idempotency.py`，保存不依赖 Flask 请求上下文的幂等预留、完成、查询和恢复逻辑。
- 新建根脚本 `run-agent-mcp.ps1` 和 `register-agents.ps1`；PowerShell 只做 Windows 入口和参数解析，配置变换调用可测试的 Python 注册逻辑。
- MCP Server 不导入数据库、存储、导出、识别服务或现有 Blueprint；业务动作全部经公开 HTTP API 完成。
- `get_service_status` 使用公开 `/health`；其余工具使用 `/api`。这是“业务能力经 `/api`”规则的唯一明确例外。

### 稳定启动器

- `run-agent-mcp.ps1` 从 `$PSScriptRoot` 推导项目根和 `.venv\Scripts\python.exe`，切换到项目根后运行 `invoice_assistant.features.agent_mcp.server`。
- 启动命令冻结为 `& $venvPython -m invoice_assistant.features.agent_mcp.server`；现有安装只安装 requirements、未把项目安装为 package，因此不得依赖任意 cwd 下的脚本导入或 console entry point。
- 启动器不得向 stdout 输出任何文本；诊断仅写 stderr，并原样转发 Python 进程退出码。
- 缺少虚拟环境、依赖或模块时立即失败，提示运行 `install.ps1` 后重新注册；不得在 MCP 启动过程中安装依赖。
- Agent 配置注册绝对启动器路径，而不是固定的 `B:\...` Python 路径。项目移动后必须重新运行注册脚本，不能猜测新位置。
- 验收从与项目无关的当前目录启动，并覆盖含空格、中文且不在 B 盘的项目副本路径。

### 固定本地端点

- 第一阶段唯一受支持地址是 `http://127.0.0.1:8765`；MCP 不接受 host/port 参数，也不读取远程 URL。
- `run.py` 先只加载 `.env.local`/`.env`，再验证端口并始终绑定 8765；`INVOICE_APP_PORT` 未设置或等于 `8765` 才允许继续，其他值必须在 `build_app()`、schema migration 和监听器之前明确失败。安装、计划任务和生产切换都验证这一条件，避免继承用户环境后 MCP 探测错端口。
- HTTP 会话设置 `trust_env=false`，并以 `allow_redirects=false` 或所选库的等价配置禁止跟随重定向，忽略系统代理环境变量。
- MCP HTTP client 对每个响应应用 4 MiB 硬上限。进程启动后先尝试核验 `/health` JSON 与 `service=invoice-assistant`，但启动 probe 只记录初始分类，`stopped/timeout/wrong_service` 不得阻断 MCP initialize、tools/list 或 `get_service_status`。每次其他业务调用都先重新核验；错误服务、HTML、重定向、超大响应及连接失败对业务工具全部 fail closed。
- `/health` 返回 503 且 JSON 可解析为发票助手时状态为 `degraded`，不是 `stopped`。
- `get_service_status` 返回 `running | degraded | stopped | timeout | wrong_service | registration_stale`；除它之外，`degraded` 或 stale registration 状态拒绝所有业务工具。
- `/health` 虽会做临时可写探针，但不提交业务状态；因此 `get_service_status` 仍可标为 read-only，并以“调用前后业务数据库和文件清单不变”的合同测试证明。
- MCP 不自动启动服务。Windows 恢复指引仅放在状态 DTO 的平台提示中，不进入平台中立错误模型。

### 超时模型

- 连接超时 2 秒；普通只读/轻量写请求 30 秒；导入、支付记录识别和导出请求 330 秒。
- Codex/Hermes 工具超时设为 360 秒，STDIO 启动/发现超时设为 15 秒，始终大于 MCP 内部截止时间。
- 首版导入一次只处理一个最多 20 MiB 的文件，避免现有 80 MiB 总请求上限、串行识别和远端重试造成不可覆盖的批量最坏时长。
- MCP 写请求不做透明自动重试。超时返回 `outcome=unknown`；只能使用原 `operation_id` 查询或重放，不能生成新 ID 猜测重试。

## 必需的 API 合同迁移

本节是明确的 `/api` 合同迁移，不得以“实现时按测试决定”代替。公开 URL 和已有响应字段继续保留；版本前置条件、新分页字段和错误码按本节增加。前端调用方与测试必须在同一里程碑同步更新。

- `Idempotency-Key` 是 Agent 写合同的 opt-in 标志：现有浏览器不发送时保留旧成功响应形状；发送时后端返回稳定的 `operation_result={resource_refs,artifact_available,warning_codes}`，MCP 只从该对象生成写工具输出。重放返回同一 status/operation_result，不重新查询后来已改变的实体。
- 版本、review 和确认字段不放 URL/query：JSON 路由（包括 DELETE）放在 JSON body，multipart 附件路由放在 form fields；`Idempotency-Key` 只放 header。缺失/重复/类型不符或 body/header 冲突都返回固定 4xx，不猜测默认值。

### 1. 持久幂等

- 所有 MCP 写工具要求 UUID v4 `operation_id`，HTTP 层以 `Idempotency-Key` 传递。
- 后端新增幂等账本，至少记录：operation ID、操作名、规范化请求指纹、状态、安全 `operation_result`、HTTP status、错误码和时间；不得保存本地路径、请求正文、财务字段、识别原文、文件内容或凭证。`operation_result` 只含资源类型/ID/提交版本、artifact 状态和 warning code，足以原样重放。
- 指纹由工具名、合同版本和所有影响行为的规范化参数生成：对象键排序、默认值先展开、整数金额保持十进制整数、数组顺序保留；排除 operation ID 和绝对路径。文件操作另包含文件 SHA-256、规范化 display basename 和附件种类，确保同内容但会产生不同业务结果的请求不会误判为相同。
- 同一 operation ID + 同一指纹：
  - 已成功时原样返回账本中的安全结果快照，`meta.replayed=true`；资源随后改变或删除也不重做副作用，调用方需另用 get 工具读取当前状态。
  - 仍在执行时返回 `operation_in_progress`，不再启动第二次业务动作。
  - 已确定失败且没有应用副作用时返回同一终态错误。
- 同一 operation ID + 不同工具、参数或文件 SHA-256 返回 409 `idempotency_mismatch`。
- 除导出外，所有带 `Idempotency-Key` 的纯数据库写入（包括 update、confirm、merge 和状态迁移）的业务变更与账本成功态都在同一数据库事务提交；现有浏览器请求若不带该 header 不创建账本。幂等命中检查先于 version/review 校验，保证已成功操作使用旧 version 重放时仍返回原结果。
- 两个文件工具都使用以 operation ID 命名的受控暂存/恢复协议，覆盖导入和 payment-record 附件的远端识别阶段。发起 DeepSeek 前先持久化 `recognition_started`；一旦进入该状态，崩溃恢复绝不自动再次外发：若已有持久化识别结果则据此提交，否则按现产品语义提交可手工处理的草稿/已保留附件并给出 warning。只有尚未进入该状态的暂存操作才可用原 key 继续；未引用文件最终清理。
- 新增受业务数据目录 ACL 保护的 `file_operation_staging` 元数据（或等价表），以 operation ID 为主键，保存恢复所需的最小可逆状态：tool/phase、文件 SHA/MIME/规范化 display basename、附件种类、目标 item 与 expected item/batch versions、受管暂存文件 ID、白名单化识别结果、已创建资源 ID/version 和时间。它可以短暂保存提交所必需的规范化财务字段，但绝不保存源绝对路径、DeepSeek raw body、凭证或自由日志文本。
- 受管暂存文件只位于 `INVOICE_APP_DATA_DIR` 的 operation staging 子目录，数据库只存不可逃逸的相对 ID。import 在最终事务中创建识别结果或 manual-fallback 草稿；attachment 可先以 `resource_applied` 阶段原子关联已保留附件，再完成识别/warning，确保恢复不会重复插入。每次恢复都重新做版本条件检查；确定成功/失败且文件关联或清理完成后删除 staging row，cleanup 未完成则保留 `cleanup_pending`，不得在 DTO/日志中暴露这些元数据。
- 因此幂等保证覆盖业务资源和“同一 operation 重放最多触发一次由业务状态机发起的 recognition dispatch”，但不宣称底层网络库、第三方处理或计费具备 exactly-once，也不承诺其结果可撤回。未来若提供用户主动重新识别，必须是新的 operation ID 和新的外发审批，不属于本阶段工具集。
- 导出继续以现有 `export_operations` 恢复协议为权威；该表补充 expected/working batch version、expected requirements version 和阶段，幂等账本关联其 token/批次结果，不能假设数分钟文件导出可放进单个数据库事务。
- 成功账本作为财务审计关联长期保留；无副作用失败记录至少保留 30 天。清理不得使旧成功 operation ID 在同一数据库中重新创建副作用。
- 新增只读 `GET /api/agent-operations/<operation_id>`，只返回操作状态和安全资源引用，不返回原始参数。

### 2. 版本前置条件

- `expense_items` 新增 `row_version`；报销包沿用已有 `row_version`，两者在安全 DTO 中统一名为 `version`。全局材料规则新增单调递增的 `requirements_version`，与规则替换同事务更新。
- Item detail 的 `batch_ref` 在已入批次时同时返回 `batch_id` 和 `batch_version`，reference data 与 item/batch detail 返回当前 `requirements_version`。任何 item/attachment 变更只要会影响批次金额、材料完整性或成员状态，就必须在同一事务中递增所属 batch version；批次聚合的完整并发前置条件是 `batch_version + requirements_version`。
- 所有会改变发票、附件、批次、材料规则或其聚合结果的持久化 mutation code path 都必须维护对应版本，包括 HTTP 路由、导出状态机、启动恢复、派生金额/完整性重算，以及现有浏览器使用的编辑、确认、合并、附件增删改/重新识别、批次创建/编辑/移除条目/删除/导出/退回/标记已报销、材料规则更新和批量删除。
- 单资源请求使用 `expected_version`；合并使用 `source_version` 和 `target_version`；创建批次使用 `[{item_id, expected_version}]`。update、merge target 和 attachment 在 item 已入批次时额外强制 `expected_batch_version`；合并进入 in-batch target 时必须重算批次总额/完整性并递增两侧版本。导出还必须提供 `expected_requirements_version` 并在事务中按当前规则重算完整性。
- 导出 reservation 同时条件检查 expected batch/requirements versions，设置 export token 并递增到内部 working batch version；token 存在期间所有批次聚合 mutation 都返回 `batch_exporting`。发布文件前和最终提交事务都再次条件检查 token、working batch version 与原 requirements version，并按当前规则重算；不匹配时绝不标记 submitted，未发布暂存物删除，已移动产物进入受控 cleanup/recovery 且对外保持不可用。
- 启动恢复只有在同一组版本/规则检查和完整性重算仍通过时才能完成 files-ready 导出；否则释放 reservation、递增 batch version 并清理产物。清理未完成时 operation 保持 `cleanup_pending/outcome=unknown`，完成后才进入确定的 `not_applied` 终态。
- 缺少实体版本返回 `version_required`，实体版本不匹配返回 409 `stale_version`；缺少/不匹配规则版本分别返回 `requirements_version_required`/409 `stale_requirements_version`。任何冲突都不得应用部分更新。
- 浏览器调用方同步发送版本并在冲突时刷新数据、提示用户重新核对。不能只保护 MCP 写入而允许旧浏览器表单覆盖新状态。
- 进程内 semaphore 只用于保持单个 MCP 进程输出顺序，不是并发正确性边界；真正保证来自数据库条件更新和事务。

需要新增实体或规则版本前置条件的现有 mutation route 闭合集合如下；import/manual 是创建类路由，不要求 expected version，但 MCP 调用仍要求 operation ID。项目名称、材料显示标签和归档目录等不改变版本化财务身份/规则判定的设置路由保持现合同：

| 现有变更路由 | 必需前置条件 | 成功后递增 |
|---|---|---|
| `DELETE /api/items/{id}` | item version | 删除，无新 version |
| `POST /api/items/bulk-delete` | 每个 item 的 ID/version | 原子删除；任一过期则全部拒绝 |
| `PATCH /api/items/{id}` | item；已入批次时再含 batch | item；如适用再含 batch |
| `POST /api/items/{id}/confirm` | item version + review token | item |
| `POST /api/items/{source}/merge/{target}` | source/target version + source review token；target 已入批次时再含 batch | source + target；如适用再含 batch |
| `POST /api/items/{id}/attachments` | item；已入批次时再含 batch | item；如适用再含 batch |
| `PATCH/DELETE /api/attachments/{id}` | owning item；已入批次时再含 batch | item；如适用再含 batch |
| `POST /api/attachments/{id}/recognize-payment` | owning item；已入批次时再含 batch | item；如适用再含 batch |
| `POST /api/batches` | 每个 item 的 ID/version | 每个 item；新 batch 从 version 0 开始 |
| `PATCH/DELETE /api/batches/{id}` | batch version | batch，或删除并更新成员 item |
| `POST /api/batches/{id}/items/{item_id}/remove` | batch + item version | batch + item |
| `POST /api/batches/{id}/export` | batch + requirements version + confirmation_name | batch + 全部成员 item |
| `POST /api/batches/{id}/reopen` | batch version | batch + 全部成员 item |
| `POST /api/batches/{id}/mark-reimbursed` | batch version | batch + 全部成员 item |
| `PUT /api/material-rules` | requirements version | requirements version；受影响聚合按新规则动态重算 |

### 3. 核对与重复项令牌

- `get_invoice_item` 的 detail DTO 返回 `review`：opaque `token`、带稳定 `uncertainty_id` 的当前 uncertainties、recognition failure、精简 duplicate candidates 和 blocking duplicate IDs；列表摘要不得返回 review token。
- token 绑定 item ID、version、完整规范化财务字段、每个阻断候选的 `{id,version,exact_file,high_confidence,historical,reason}` 摘要，以及当前不确定项/阻断集合；任一候选内容、状态或集合变化后旧 token 必须返回 `review_changed`。
- duplicate correctness 不得依赖当前实现的 `LIMIT 50` 或展示上限：exact-file 及其他阻断候选先做完整查询/计数。MCP detail 最多展示 100 个阻断候选；总数超过上限时返回 `duplicate_review_overflow=true` 和 `blocking_total`，token 绑定完整集合摘要、总数及 overflow，MCP confirm/merge 一律以 `duplicate_review_overflow` fail closed。
- 为避免 overflow 草稿成为死路，新增仅供现有浏览器 UI 使用、但不注册为 MCP 工具的 `POST /api/items/{id}/duplicate-review-sessions`（要求 expected item version + 当前 review token）与 `POST .../{session_id}/next`（要求上页 cursor）：以 100 条 keyset page 顺序返回完整阻断集合，并维护 15 分钟、item/version 绑定的 server-side review session（仅存集合 digest/count、分页进度和过期时间）。跳页不可完成 session；读取最后一页后签发一次性 `overflow_review_token`。
- 浏览器 confirm/merge 可用该 token 代替超长 ID 列表；写事务仍重算完整候选 digest/count/version，token 过期、未读完、候选变化或 target 不在绑定的 merge-allowed 集合时零副作用拒绝。MCP schema 不接受该 token，也不暴露 review-session endpoint。
- `confirm_invoice_item` 只执行状态确认，不同时悄悄改财务字段；修改必须先调用 `update_invoice_item`，重新读取后再确认。
- 确认必须传 `expected_version`、`review_token` 和 `duplicate_resolution`。存在不确定项时，`acknowledged_uncertainty_ids` 必须精确等于当前阻断集合；无阻断重复时 resolution 必须为 `none` 且 duplicate IDs 为空，存在 exact-file/high-confidence/历史阻断时则必须为 `keep_separate`，且 `acknowledged_duplicate_ids` 精确覆盖当前阻断集合。
- 待确认草稿可通过 `merge_invoice_draft` 合并到可修改目标；参数明确使用 `source_review_token`、两侧 ID/version。`source_id` 必须不同于 `target_id`，且 target 必须是该 token 绑定并标记 `merge_allowed=true` 的当前 duplicate candidate；任意非候选、历史项或已不可修改目标分别以固定 `invalid_merge_target`/`merge_target_not_reviewed` 返回且零副作用。历史记录只能在明确 keep-separate、精确 resolution/acknowledgement 和客户端风险门槛后确认。
- confirm/merge 在 `BEGIN IMMEDIATE` 或等效写串行化边界内重新计算 review token 和阻断重复集合，再完成条件更新；所有会改变重复判定输入的创建/编辑路由使用同一边界，不能在事务外核对后再提交。
- review token 使用带合同版本的 canonical JSON 摘要生成，序列化规则固定并做常量时间比较；它可跨进程重算、客户端必须当 opaque value 使用。它不是 bearer credential，即使摘要可推导也不能绕过 version、事务内重算或客户端风险门槛。
- review token 只证明核对的是当前快照，不证明真人同意。Codex 配置 `writes` 风险审批；Hermes `trust: untrusted` 在当前 `0.20.1` 中会对每一次非只读工具调用请求批准，且不能永久放行。两者的实际行为与局限都必须在实机验收中展示。

### 4. 可分页列表

- `drafts`、`items`、`batches` API 增加可选 `limit` 和 opaque `cursor`；旧浏览器不传时保持现有响应兼容，MCP 始终使用新分页合同。
- MCP `limit` 默认为 50，范围 1..100；分页只放在 `meta.pagination={next_cursor,has_more}`，不得在 `data` 中复制，也不得静默截断到 500 条。
- 三类列表均按不可变 `(created_at,id) DESC` 做 keyset，并把首屏 `snapshot_max_id` 和 filter hash 带入 cursor；遍历期间新增行不进入本轮，删除/状态迁移的行可以消失，调用方需要绝对新鲜结果时重新开始一轮。
- cursor 严格校验版本、字段结构、类型和 filter hash；非法或与筛选条件不匹配时返回 `invalid_cursor`。第一阶段不把它描述成带 MAC 的安全令牌。

## MCP 公共工具合同

### 统一规则

- 第一版 manifest 固定为下表 17 个工具；显式配置 allowed roots 时 exposed set 为 17 个，未配置时 exposed set 为 15 个并移除两个文件工具。`contracts.py` 的单一 manifest 同时生成 MCP 注册、客户端 allowlist 和两种合同测试，避免三处漂移。
- 所有输入/输出 Schema 使用 JSON Schema 2020-12；每个自有 DTO 对象递归设置 `additionalProperties=false`，确需动态 map 的字段单独声明。实体写入只接受显式 ID，不允许按列表位置推断。
- 所有写工具必须有 `operation_id`；所有修改已有实体的工具必须有对应 version；相同参数重放才可声明 idempotent。
- 所有金额输入和输出统一使用整数 `amount_cents`；换算金额使用整数 `converted_amount_cents`，并携带 currency。这里冻结现有应用“所有币种均按 1/100 计”的口径，不按 ISO 货币指数推导；范围沿用 `0..10_000_000_000_000` cents，草稿可为 0、确认/导出所需金额必须大于 0。JSON 浮点一律拒绝，适配现有 API 时只用 Decimal/十进制字符串转换。
- `import_invoice_file` 以及 attachment kind 为 payment record 时，输入 Schema 强制 `external_processing_notice_version="deepseek-v1"` 与 `external_processing_ack=true`，tool description 固定显示：“若本地后端已配置识别凭据，此文件内容将发送至 DeepSeek 进行识别；未配置时仅保存为手工处理记录。”缺失/旧版本确认在读取或外发前返回 `external_processing_ack_required`；该布尔值只是合同防旧客户端门槛，不冒充真人证明。
- 写工具通过客户端风险门槛和后端业务校验共同保护；ToolAnnotations 只是风险提示，不是授权边界。Hermes 原生逐次审批 prompt 不显示 tool description，因此验收必须证明 Agent 在调用前的可见对话中展示上述固定外发披露，再由用户批准该次写调用；不能声称原生 prompt 自身含 DeepSeek 文案。

| Tool | HTTP 映射 | 关键前置条件 | R | D | I | O |
|---|---|---|---:|---:|---:|---:|
| `get_service_status` | `/health` | 无 | 1 | 0 | 1 | 0 |
| `get_invoice_reference_data` | `/api/bootstrap` | 字段白名单 | 1 | 0 | 1 | 0 |
| `get_dashboard_summary` | `/api/dashboard` | 字段白名单 | 1 | 0 | 1 | 0 |
| `get_agent_operation` | `/api/agent-operations/{operation_id}` | operation ID | 1 | 0 | 1 | 0 |
| `import_invoice_file` | `/api/imports` | 单文件、allowed root、operation ID、DeepSeek notice/ack | 0 | 0 | 1 | 1 |
| `create_manual_invoice_draft` | `/api/items/manual` | operation ID、整数 amount_cents | 0 | 0 | 1 | 0 |
| `list_invoice_drafts` | `/api/drafts` | 分页 | 1 | 0 | 1 | 0 |
| `get_invoice_item` | `/api/items/{id}` | 显式 ID | 1 | 0 | 1 | 0 |
| `update_invoice_item` | `PATCH /api/items/{id}` | operation ID、item version；in-batch 时再含 batch version | 0 | 1 | 1 | 0 |
| `confirm_invoice_item` | `POST /api/items/{id}/confirm` | operation ID、version、review token | 0 | 1 | 1 | 0 |
| `merge_invoice_draft` | `POST /api/items/{source}/merge/{target}` | operation ID、两侧 version、source review token；target in-batch 时再含 batch version | 0 | 1 | 1 | 0 |
| `list_invoice_items` | `/api/items` | 筛选、分页 | 1 | 0 | 1 | 0 |
| `add_invoice_attachment` | `POST /api/items/{id}/attachments` | 单文件、allowed root、operation ID、item version；in-batch 时再含 batch；payment kind 再含 notice/ack | 0 | 1 | 1 | 1 |
| `create_reimbursement_batch` | `POST /api/batches` | operation ID、item/version 列表 | 0 | 1 | 1 | 0 |
| `list_reimbursement_batches` | `/api/batches` | 筛选、分页 | 1 | 0 | 1 | 0 |
| `get_reimbursement_batch` | `/api/batches/{id}` | 显式 ID | 1 | 0 | 1 | 0 |
| `export_reimbursement_batch` | `POST /api/batches/{id}/export` | operation ID、batch/requirements version、confirmation_name | 0 | 1 | 1 | 0 |

`R/D/I/O` 分别代表 `readOnlyHint`、`destructiveHint`、`idempotentHint`、`openWorldHint`。创建批次和导出会推进已有状态；更新、确认、合并和附件操作也会改变已有对象，因此不能标记为“仅增量、非破坏性”。导入及支付记录附件之所以可标 idempotent，是因为持久化 dispatch marker 保证同 operation 重放不新增远端 dispatch；这不承诺第三方传输/计费可撤销或 exactly-once。两者保守标记为 open-world，并在 tool description、输入确认字段和调用前可见披露中明确说明文件可能经后端发送给 DeepSeek。

### DTO 和错误模型

- 成功 envelope：`schema_version`、`ok=true`、`data`、`warnings`、`meta`；meta 仅含 request ID、operation ID、replayed 和分页信息。
- 失败 envelope：`schema_version`、`ok=false`、`error={code,message,http_status,retryable,outcome}`；`outcome` 只取 `not_applied | unknown`。
- MCP `isError` 与 `ok=false` 保持一致；不得把 HTTP 200 的业务失败伪装成成功。
- 所有写工具的 `data` 只返回资源类型/ID/提交版本与必要的 `artifact_available`；可恢复识别问题放入结构化 warning。完整可变实体必须再用 get 工具读取，从而让幂等重放可返回相同、非敏感的固定结果。
- 导出参数 `confirmation_name` 必须按 Unicode code point 与最新 batch detail DTO 中的完整 `name` 完全相等，不 trim、case-fold 或隐式规范化；不匹配返回 `batch_confirmation_mismatch`，且不得开始导出。
- 金额字段遵循统一规则中的 `amount_cents`/`converted_amount_cents`，不输出用于计算的浮点金额；日期为 ISO 8601，时间为 RFC 3339 UTC。
- Item 仅暴露：ID/version、核对所需财务字段、项目、状态、不确定项、材料完整性、批次引用和精简附件。
- Batch 仅暴露：ID/version、requirements version、名称、项目、状态、整数金额、完整性和 `artifact_available`；不返回本地归档或 PDF 路径。
- 递归禁止 `ai_raw`、`confirmed_snapshot`、`audit_logs`、`managed_path`、`archive_path`、`pdf_path`、数据库字段、环境变量和密钥。
- 可返回清洗后的附件 display name；不得返回绝对路径、allowed roots 或目录枚举。
- 识别结果和文件名都按 untrusted data 处理：入库和计算 token 前先按同一规则移除控制字符并做字段级规范化，不渲染可点击外部链接，不把其中内容当成指令。detail 中所有参与 review token、uncertainty acknowledgement 或 duplicate decision 的字段必须完整返回、不得截断；只有 list summary 可按固定上限截断，并在 `truncated_fields` 中逐字段标明。
- HTML、堆栈、HTTP body 和内部异常只写安全错误码；超时和连接错误必须区分“请求未发送”与“结果未知”。

## 文件访问安全

- allowed roots 默认空；`register-agents.ps1 -AllowedRoot <path[]>` 显式授权。未传根目录时，客户端 allowlist 和 MCP discovery 都不包含 `import_invoice_file` 与 `add_invoice_attachment`。
- roots 由注册脚本序列化为 JSON 数组，写入两客户端 MCP 条目的 `env.INVOICE_MCP_ALLOWED_ROOTS_JSON`；空授权显式写 `[]`，重复注册时完整替换旧值，不能用 Windows 分隔符手工拼接。Server 只解析该键；缺失、非数组、非字符串元素或非法 JSON 均按空 roots fail closed、仅向 stderr 输出安全诊断。
- 注册时根目录必须存在、是本地磁盘上的普通目录、不是 reparse point；记录大小写折叠后的 canonical path。
- 文件工具只接受一个绝对路径，支持 `.pdf`、`.png`、`.jpg`、`.jpeg`、`.webp`，大小范围 `1..20 MiB`；后端仍是格式、magic、PDF 和 Pillow 校验的最终权威。
- 拒绝相对路径、越界、路径前缀碰撞、UNC、设备命名空间、ADS、目录、非普通文件、硬链接，以及任一路径组件上的 symlink/junction/reparse point。
- 使用 strict resolve、按路径组件比较和大小写无关的 `commonpath` 判定，禁止字符串 `startswith` 授权。
- 打开后从文件句柄取得最终路径、文件类型、link count 和稳定 identity，重新证明它仍位于 allowed root 内且不是 reparse/hardlink/非普通文件；读取完成后再次核对 identity，任一变化都拒绝。
- 从同一已验证句柄最多读取 `20 MiB + 1 byte`，多出的 1 byte 只用于判定超限并立即拒绝，绝不能把超大文件静默截断成合法输入；合法文件一次性读入缓冲区并计算 SHA-256，上传不再按路径重新打开。
- MCP 不枚举根目录，不在 DTO 中返回根目录、相邻文件或绝对路径；stderr 日志连完整文件名也不记录。
- 项目目录和用户主目录均不享有隐式信任。用户如确需授权，必须像其他目录一样显式传入精确根。

## Windows 客户端注册

### 依赖和命令面

- 运行依赖增加 `mcp>=2,<3`；格式保留式配置编辑增加 `tomlkit>=0.13,<1` 和 `ruamel.yaml>=0.18,<0.19`。实现时记录实际解析版本并验证与项目 Python 兼容。
- `register-agents.ps1` 参数只包含：
  - `-Agent all|codex|hermes`
  - `-AllowedRoot <path[]>`
  - `-StateRoot <absolute-path>`（可选；默认见下文，自动化必须传临时目录）
  - `-Remove`
- 不提供 `-Port`，也不提供覆盖任意同名用户配置的 `-Replace`。
- 服务名固定为 `invoice_assistant`。

### 安全更新语义

- 首次注册遇到未受本脚本管理的同名条目时拒绝并给出重命名/手工处理说明，不覆盖用户配置。
- Codex 配置、Hermes 配置和 registration state 共同构成一次补偿式注册事务。默认 state root 为 `%LOCALAPPDATA%\InvoiceAssistant\agent-registration\`；`-StateRoot` 必须是绝对本地目录并创建/验证为当前用户私有 ACL。已受管条目不得就地迁移 state root，必须先用原 root 安全 `-Remove` 再注册。状态和 journal 都不得包含原配置、密钥或其他 Server 内容。
- 注册成功后，受管状态保存每个目标的 canonical hash、配置路径、客户端类型、随机 registration generation、lease 文件和时间；prepare/commit journal 另记录事务 ID、阶段、原文件是否存在、原 SHA-256 和受控备份路径，以便脚本重启时恢复未完成事务。prepare journal 必须在首次 replace 前以 sibling temp + flush/fsync + atomic replace 持久化；受管状态/lease 写完后再持久化 commit phase，最后才清理 journal。
- 重复运行且目标相同为无操作成功；若现有条目 hash 与受管状态一致，可安全更新项目路径、roots 和工具 allowlist。
- 若用户在注册后手工修改该条目，hash 不匹配时更新和 `-Remove` 都必须拒绝，避免删除用户的新配置。
- 按规范化绝对路径排序取得所有 sibling lock；任一锁失败均不开始写入。锁内保存原文件是否存在、原始字节/SHA-256 和目标条目 canonical hash，先完成所有客户端的能力探测、解析和 staging 校验。
- 每个 sibling staging 文件 flush/fsync 后重新解析，并证明除 `invoice_assistant` 条目外原字节语义不变；替换前再次比较目标 SHA-256，发现外部并发修改立即失败。已存在文件用 `.NET File.Replace(stage,target,uniqueBackup)`，原不存在文件用同目录原子 rename；no-op 不创建备份且 hash/mtime 不变。unique backup 仅供本事务补偿，不是历史备份。
- journal 同时保存本事务写入后的 expected SHA-256。失败补偿或下次恢复前先做 CAS：当前文件等于事务写入 hash 才可恢复，等于 original hash 则视为已恢复；若两者都不等，说明 replace 后发生外部修改，禁止覆盖/删除，保留 journal 与备份并返回 `51` 供人工合并。原先不存在的目标也只在当前 hash 等于事务写入 hash 时删除。
- 配置替换后依次做客户端解析、目标条目 canonical 比较和独立 MCP `initialize`/`tools/list`；全部目标通过后才原子写受管状态。不能只以 CLI 退出码判定成功。
- 任一阶段失败，或下次启动发现未到 commit phase 的 journal，按反序将所有已触碰配置和受管状态恢复到原字节/原不存在状态；已到 commit phase 时重新验证配置与状态。补偿或成功提交完成后都删除 unique backups，再清理 journal，不长期保留包含整份客户端配置的敏感副本；删除失败则保留受私有 ACL/journal 管理的路径、返回 51 并在下次运行优先重试清理。恢复失败同样返回 51、保留并报告受控备份路径并禁止继续注册。这里是可恢复的补偿事务，不宣称多个文件具有文件系统级原子性。
- `-Remove` 只移除 hash 匹配的受管条目和对应状态；不存在时为无操作成功，不修改其他 Server。
- `-Remove` 不依赖客户端 executable 是否仍安装：按受管 state 记录的配置路径/canonical hash 处理并先撤销 lease；配置已不存在则清理状态，配置仍匹配则移除目标条目，漂移则保留用户字节、保持 lease revoked 并返回 30。
- 每次更新、roots 收窄或移除都原子轮换/撤销 lease generation。客户端 env 键固定为 `INVOICE_MCP_REGISTRATION_LEASE_PATH` 和 `INVOICE_MCP_REGISTRATION_GENERATION`；已启动 MCP 若发现 generation 与 lease 内容不同或 lease 消失，只允许 `get_service_status` 返回 `registration_stale`，其余新调用全部 fail closed；已经发出的调用不能声称可撤回。
- 脚本成功后必须明确要求关闭/refresh 对应 Codex/Hermes 会话并新建会话；验收同时证明旧进程后续业务调用因 stale lease 失败、新进程 discovery 精确匹配新 allowlist。未完成刷新前不得宣称 remove/roots 收窄已完全生效。
- “未安装即 skipped”只适用于从未受管的首次注册：`all` 可跳过，显式选择返回 20，两个都缺失时 `all` 返回 20。若已有受管条目但 executable 缺失，更新不得 skipped；先原子撤销其 lease、保持其他配置不变并 fail closed 返回 20，提示可在无需 executable 的 `-Remove` 中清理。已安装但能力不足返回 21 且不得留下部分配置。
- 固定退出码：`0` 成功/no-op；`20` 首次显式客户端缺失、all 全部缺失或受管客户端 executable 丢失；`21` 能力不支持；`30` 同名冲突/受管漂移；`40` 锁、读取、解析或 staging 失败；`50` 后置失败且补偿成功；`51` 补偿或敏感备份清理未完成、需要按报告人工处理。

### Codex 配置

- 通过 `CODEX_HOME`（未设置时使用默认用户目录）定位 `config.toml`，保留其他 TOML 内容和注释。
- 注册绝对 PowerShell 路径，参数固定为 `-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <absolute-run-agent-mcp.ps1>`，并设置 `cwd=<project-root>` 作为额外防护；Bypass 只作用于该子进程。
- 配置 `env` 固定包含 `INVOICE_MCP_ALLOWED_ROOTS_JSON`、`INVOICE_MCP_REGISTRATION_LEASE_PATH` 和 `INVOICE_MCP_REGISTRATION_GENERATION`；设置单一 manifest 生成的 `enabled_tools`、`default_tools_approval_mode="writes"`、`startup_timeout_sec=15`、`tool_timeout_sec=360`。
- 配置后用 `codex mcp get invoice_assistant --json` 和真实工具发现验证；不得只检查文本是否写入。

### Hermes 配置

- 通过 `HERMES_HOME`（未设置时使用默认用户目录）定位 `config.yaml`，保留其他 YAML 内容和注释；command/args 同样使用绝对 PowerShell 路径和 `-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File`，`env` 写入与 Codex 相同的 roots/lease/generation 三项。
- 当前 `hermes mcp configure` 是交互命令，注册脚本不得在无人值守流程调用它；使用格式保留式 YAML 编辑后再调用非交互的 `hermes mcp test invoice_assistant` 验证。
- 设置 `trust: untrusted`、`timeout: 360`、`connect_timeout: 15`、`supports_parallel_tool_calls: false`、manifest 生成的 `tools.include`、`tools.resources: false`、`tools.prompts: false`。
- 不仅按版本字符串判断兼容性；在临时 `HERMES_HOME` 中实际验证目标配置可解析，并由独立 MCP 客户端确认精确非空工具集合，再在全新 Hermes 会话中观察只读直通、每次写调用进入原生批准/拒绝路径。`hermes mcp test` 退出码不能替代这些证明。
- 当前本机 Hermes `0.20.1` 已通过逐次写审批 gate 的本地纯函数证明，但这不等于真实会话验收；若最终隔离/实机能力探测无法可靠证明该 gate fail closed，则不得注册该客户端。任何客户端升级都需要用户显式操作，是计划外部前置条件；Codex 子集可继续，但本 Plan 保持 `Active`。

### 安装和卸载生命周期

- `requirements.txt` 中的新运行依赖继续由现有 `install.ps1` 安装，但安装脚本不调用注册脚本。
- 根 README 说明注册、更新、移除、allowed roots、固定端口、服务不会被 MCP 自动启动，以及项目移动后必须重新注册。
- `uninstall.ps1` 若发现受管注册状态，只打印先运行 `register-agents.ps1 -Agent all -Remove` 的提示；不在卸载应用入口时静默改写外部客户端配置。

### 独立生产迁移命令

- 新增 `python -m invoice_assistant.migrate --data-dir <absolute-path> (--expect-db-sha256 <sha256> | --expect-no-database)`。`run.py` 在 `build_app()` 前获取由 canonical data root hash 派生的 Windows named mutex（不在数据树创建 lock file）；migrate 在任何写入前核对数据根、数据库存在性/hash 并独占同一 mutex，随后只执行可重复的 schema/data migration，并运行 `integrity_check`、foreign-key check 和预期 schema version 校验。
- migration 逻辑从 `init_db()` 抽成无 Flask 请求/服务上下文的 callable，所有 DDL/backfill 在显式 `BEGIN IMMEDIATE` 中执行且 `user_version` 最后更新，任一异常回滚；不得继续使用会隐式切断事务的脚本路径。该命令不得调用 `create_app()`，不得启动 HTTP、DeepSeek、export recovery、cleanup queue、trash purge 或自动备份。以执行时当前 DB hash 重新调用时，已迁移数据库必须是无操作成功；过期 expect hash 则在写前拒绝。
- migrate 是唯一允许创建或升级业务 schema/data 的入口。`prepare_data_dir` 的旧数据复制也移入该命令；`run.py/build_app/create_app` 只做无副作用路径解析，并以 SQLite read-only/no-create 连接校验 schema/version。数据库缺失、`user_version` 落后或表/列不符时，必须在创建日志/目录、打开写事务、recovery/cleanup/autobackup 之前以 `migration_required` 非零退出，启动前后 DB/WAL/SHM 和数据文件树字节不变；日志 handler 只能在校验通过后创建。
- Fresh install 只有在 data root 不含数据库、受管文件或 operational state 时才允许 `--expect-no-database`。`install.ps1` 可对这一严格空状态显式调用 migrate；发现已有但落后的数据库时只停止并打印恢复集 + hash migration 指引，绝不自动升级。测试 fixture 同样先显式 migrate，不能依赖 `create_app()` 隐式建库。
- 生产顺序冻结为：停止计划任务/客户端写入口 → 一致恢复集 → 部署已验收源码/依赖 → 运行上述 migrate 命令 → 以前台方式启动固定 8765 服务，让现有受控 recovery/cleanup 跑完 → 核验 `/health` 和浏览器只读流程 → 停止前台实例 → 正式注册/刷新客户端 → 恢复计划任务。任一步失败都不得越过下一门槛。

## 实施里程碑

### M0 — 发布 Plan 与冻结合同

- 将本计划放入 `docs/plans/`，更新索引并设为 `Active`。
- 重跑全量 pytest 和 JavaScript 语法基线，记录实际结果。
- 将 17 个工具、HTTP 端点、审批、版本、幂等、DTO 字段和错误码固化成测试用 manifest。
- 在临时配置根中完成 Codex/Hermes 能力探测；不兼容客户端只报告外部升级前置条件，不由实现脚本自动升级。
- 实施只在独立项目副本、独立虚拟环境、临时 `INVOICE_APP_DATA_DIR` 和临时客户端配置根中进行，并以源树 hash guard 证明生产项目根未被修改；这样生产任务可在不占用隔离验收端口时继续运行旧版本。凡需使用固定 8765、制作一致恢复集或无法保证副本隔离的阶段，先停止生产任务；直到 M5 明确切换前，生产服务绝不能加载半成品迁移代码。
- 在第一次生产 schema migration 前、所有写入口停用且文件队列已对账时，关闭数据库连接并执行/验证 WAL checkpoint，再在项目/数据/归档目录之外制作一致恢复集：完整 `INVOICE_APP_DATA_DIR`、外部 archive root（去重重叠路径）、不可变旧源码包及 SHA-256、Python 版本/架构、`pip freeze --all` 和对应离线 wheelhouse。源码包排除 `.venv`、数据、`.env` 与凭证；恢复集不得提交到仓库。
- 本项目当前不是 Git worktree，故 commit 名不能替代旧源码包。M0 必须把恢复集还原到隔离路径，使用锁定依赖启动旧版本，运行数据库/文件 reconciliation、`/health` 和只读浏览器烟测，并记录命令、校验和、存放位置与容量；空间或权限不足时生产切换被阻断。

Exit gate：所有写工具都有明确原子性、重试、并发和核对语义；不存在“实现时再决定”的合同。

### M1 — API 一致性基础

- 完成无 Flask 上下文的独立 migration 命令，以及数据库 additive migration：item version、requirements version、幂等/文件 staging/review-session 表及所需索引。
- 实现条件写入、操作状态、review token 和 keyset 分页。
- 同步更新所有现有浏览器变更调用和 API 测试。

Exit gate：HTTP 层单独证明同 key 重放、异参冲突、过期版本拒绝、重复核对变化和无部分提交；迁移前一致恢复集已在隔离目录完成 DB/文件一致性恢复，且尚未让旧应用或生产任务接触迁移后的真实数据。

### M2 — MCP 只读核心与 DTO

- 先实现状态、reference、dashboard、operation、list/get 工具。
- 建立统一 Schema、DTO、错误模型、日志脱敏和 STDIO stdout 约束。

Exit gate：内存客户端和真实 STDIO 子进程均能发现工具并验证 structured output；未注册任何写工具。

### M3 — 写工具与文件边界

- 实现单文件导入、手工草稿、更新、确认、合并、附件、批次和导出。
- 完成 allowed roots、同句柄有界读取、DeepSeek open-world 描述/输入确认/调用前披露和所有风险注解。

Exit gate：所有写合同、路径攻击、超时/结果未知和进程恢复测试通过，才允许 manifest 暴露写工具。

### M4 — Windows 启动与注册

- 完成稳定启动器、格式保留式配置编辑、受管 hash、prepare/commit journal、崩溃恢复、备份、移除和补偿式事务回滚。
- 自动化只使用临时 `CODEX_HOME`/`HERMES_HOME`/`-StateRoot` 和 CLI stub/真实可执行文件的隔离配置，绝不修改真实用户配置或默认 registration state。

Exit gate：临时配置中的重复注册、受管更新、同名冲突、手工漂移、移除、客户端缺失、能力不足、并发修改以及每个 journal 阶段的故障/进程中断恢复全部通过。

### M5 — 隔离集成与 Windows 实机验收

- 用临时 Flask 服务、`INVOICE_APP_DATA_DIR` 和识别 stub 完成 MCP → HTTP → 业务闭环。
- 在真实 Codex/Hermes 中完成发现、只读调用、Codex writes policy、Hermes 逐次写调用审批、调用前外发披露和完整财务流程。
- 完成浏览器状态验证、一致恢复集演练和注册事务恢复，并把实际命令、客户端版本、输出摘要和限制写入 owning Plan。
- 隔离门槛全部通过后才允许用户选择生产切换：保持计划任务停用，先对尚未覆盖的旧生产源码/依赖/数据/归档重新做并校验一致恢复集，再将同一已验证版本部署到生产项目根，执行 migration 和只读健康/浏览器烟测，最后显式注册指向生产 launcher 的客户端并恢复计划任务；任何新合同写入前失败可按恢复集整体恢复。

Exit gate：本计划全部自动化、两个目标客户端的 Windows 门槛和用户明确选择的生产切换均通过，且正式注册只指向生产 launcher，才将状态改为 `Implemented`；用户暂不切换时保持 `Active`。

## 验证矩阵

### 协议与合同

- 使用 MCP SDK v2 客户端验证现代 discovery，并验证其兼容旧式初始化的 STDIO 路径；stdout 不含日志、BOM 或提示文本。
- 逐工具断言 name、input/output Schema、递归 `additionalProperties=false`、ToolAnnotations 和 structured content；有 roots 时集合精确为 17，无 roots 时精确为 15 且只缺两个文件工具。
- 断言 import/payment-record attachment 的 Schema 必须含固定 notice version/ack，普通附件不误触发外发；缺失确认时后端在文件读取/DeepSeek dispatch 前拒绝。
- 断言 resources、prompts、删除、退回、已报销、设置、打开目录和报价工具均不存在。
- 对 DTO 递归断言禁用字段不存在；构造超长/带控制字符/伪指令的识别文本，验证 detail 核对字段经规范化后无损返回、list summary 才截断并准确列入 `truncated_fields`。

### 幂等与并发

- 同 operation ID 同参数重放只产生一次副作用；异参、异工具或不同文件返回 `idempotency_mismatch`。
- 对每一种纯数据库写覆盖“业务与账本同提交”，并覆盖数据库提交后响应丢失、旧 version 成功重放、两个 MCP 进程并发同 key、进行中查询和进程重启恢复；两个文件工具分别覆盖每个 staging/识别/提交恢复点。
- 在持久化 `recognition_started` 前、后及远端响应持久化前后注入崩溃；同 key 重放不得静默产生第二个 recognition dispatch，未知结果必须落为 manual-fallback 草稿/已保留附件 warning。新的识别尝试必须使用新 operation ID 并重新经过外发审批。
- 逐 phase 校验 `file_operation_staging` 的最小字段、ACL、相对 staging ID、版本冲突、resource_applied 恢复、terminal 清理和 cleanup_pending 重启；DTO/日志/幂等账本不得泄露 staging metadata 或其中的财务字段。
- 两个客户端使用同一 version 写同一 item：仅一个成功，另一个得到 `stale_version`。
- 浏览器先修改后 MCP 写、MCP 先修改后旧浏览器提交，后提交方都必须刷新而不能覆盖。
- 两个批次争抢同一 item：仅一个成功；失败方不留下空批次或部分 item 状态。
- 对 in-batch item 的 update/merge/attachment 同时竞争 item/batch version，仅一个事务成功且批次金额、完整性和两侧版本一致。
- 材料规则在读取批次后变化时，旧 `requirements_version` 的导出得到 `stale_requirements_version`；刷新后按新规则重新计算材料完整性。
- 导出开始后并发修改材料规则或尝试修改成员/附件，最终提交不能使用过期规则或聚合；失败/恢复路径不得留下可用的孤立归档。
- 导出并发/重放不会生成第二套归档；每个中断恢复点仍以扩展后的 `export_operations` 状态机为准，并正确递增 batch/item versions、拒绝旧 version。

### 核对与财务语义

- 识别成功、识别失败但草稿已保存、手工草稿、update 后重新核对、确认及重复确认。
- 新增/编辑重复项、同一候选 version/状态变化或并发创建 exact-file 候选后旧 review token 失效；exact-file、高置信候选、历史重复分别走 keep-separate 或 merge 的允许路径。即使两侧 version 正确，自合并以及 token 未绑定的任意 target 也必须拒绝且零副作用。
- 构造超过当前查询 limit（含第 11/51 个）的 exact-file/high-confidence 阻断候选，验证完整计数、overflow fail closed，不能因展示截断而确认或合并。
- 浏览器 overflow review 覆盖完整顺序翻页、跳页、过期、重复使用、候选并发变化、keep-separate 和 merge target；只有完成 session 后的有效 token 可解除 overflow，MCP 始终不可使用该通道。
- 不确定金额、主体、日期、用途和项目未经精确 acknowledgement 不得确认。
- 普通附件与 payment record；后者识别失败时保留附件并返回 warning，不把已应用结果标记为可重试失败。
- 材料缺失阻止导出；错误或大小写/空白变化的 `confirmation_name` 返回 `batch_confirmation_mismatch` 且无副作用。
- 金额输入拒绝 JSON float，覆盖 `0.1` 对应的十进制转换、大额边界、负数/溢出规则及指纹稳定性；创建批次、完整导出和重复导出均使用固定 1/100 的整数金额口径。

### 文件和传输安全

- 覆盖空文件、恰好 20 MiB、20 MiB+1 byte、扩展名/内容不符、路径大小写、`..`、前缀碰撞、UNC、device、ADS、symlink、junction、reparse point、硬链接和检查前/打开后/读取中交换文件；验证超限是拒绝而非截断。
- 覆盖 allowed root 含空格/中文、非 B 盘、根列表替换，以及未配置 roots 时文件工具完全不可发现。
- 设置 HTTP(S)_PROXY 后确认请求仍直连 loopback；重定向、错误 service、HTML、非 JSON、过大响应、503 degraded 和超时均 fail closed。
- 服务完全停止和 8765 被错误服务占用时，MCP initialize/tools/list 仍成功且 `get_service_status` 分类准确，其他业务工具拒绝；`INVOICE_APP_PORT` 为非 8765 时 `run.py` 在 app/migration 前失败。
- 日志捕获断言不含参数正文、搜索词、发票字段、文件名、路径、HTTP body、环境变量、客户端配置或凭证。

### 注册隔离

- 自动化测试给 Codex/Hermes 使用临时配置根和显式临时 `-StateRoot`；执行前后真实用户配置及默认 `%LOCALAPPDATA%\InvoiceAssistant\agent-registration\` tree hash 必须不变。
- 覆盖配置不存在、空配置、复杂注释、其他 MCP Server、CRLF/Unicode、严格 no-op、受管更新、未受管同名冲突、手工漂移、外部并发修改、移除和回滚。
- 对单客户端和 `all` 分别在 lock/parse/stage/flush/replace/client-validation/state-write/commit-marker/journal-cleanup 各阶段注入失败及进程中断；下次运行后，未 commit 的事务精确恢复到原字节/原不存在状态，已 commit 的事务验证后保持完整新状态，绝不混合。
- 注入“脚本 replace 后、失败/重启前被用户或客户端再次修改”的竞态；CAS 必须保留外部新字节、返回 51 并报告受控备份，不能用旧备份覆盖。
- 成功/no-op/补偿成功后断言没有遗留 unique backup；注入 backup 删除失败时必须由 journal 索引、返回 51，下一次运行清理后才移除 journal。
- `hermes mcp test` 之外必须由独立 MCP 客户端断言 initialize、tools/list 和精确集合；自动化执行前后真实用户配置 hash 不变。
- 断言两客户端配置精确写入 roots JSON/lease/generation env；缺失或畸形 roots 只暴露 15 个工具。更新、收窄 roots 和 `-Remove` 后，旧 STDIO 进程的新业务调用得到 `registration_stale`，关闭/refresh 后的新会话才发现精确新集合。
- 分别覆盖“从未受管且 executable 缺失”的首次 skip/20、“已有受管条目但 executable 后来缺失”的更新撤销 lease/20，以及无需 executable 的 hash-matched `-Remove`；重装客户端后不得复活潜伏条目。
- 从含空格/中文且无关的 cwd 通过完整 PowerShell args 启动，证明实际执行 `.venv\Scripts\python.exe -m invoice_assistant.features.agent_mcp.server`，并覆盖 Mark-of-the-Web/Restricted policy 等价测试。

### 项目级回归

- 独立 migrate 命令覆盖错误/过期 DB hash、服务锁占用、严格空 fresh database、非空无 DB 目录、旧 schema、重复 no-op、故障回滚和完整性检查；断言不会触发 HTTP、DeepSeek、export recovery、cleanup、trash 或 autobackup。
- 直接以 missing/旧 schema 启动 `run.py/create_app` 必须返回 `migration_required`，且 DB bytes、WAL/SHM 和完整文件树 hash 前后不变；所有测试 fixture 显式 migrate 后才创建 app。
- 后端/API：`.\.venv\Scripts\python.exe -m pytest -q`。
- 对 `web/` 下每个 `.js` 文件执行 `node --check`。
- 任何前端可见状态、版本冲突或流程变化都必须在真实浏览器中覆盖录入、核对、批次、附件和导出，Console 无错误；静态检查不能代替浏览器证据。

### Windows 实机流程

1. 备份客户端配置，停止现有计划任务服务和全部写入口，完成 pending operation/export/cleanup 对账，并按 M0 规则制作一致恢复集。
2. 在 `127.0.0.1:8765` 启动使用临时 `INVOICE_APP_DATA_DIR` 的隔离实例；显式把 `DEEPSEEK_API_KEY` 置空，先验证无远端调用的手工降级闭环。
3. 从无关当前目录分别用全新 Codex、Hermes 会话完成 MCP discovery；记录 Codex `writes` 的实际风险提示，以及 Hermes 只读调用直通、每次写调用均要求批准且拒绝后不执行的实际行为。
4. 每个客户端分别完成：单文件导入、读取草稿、修改、重新读取 review、确认、重复处置、创建批次、添加材料、导出、幂等重放。
5. 停止隔离业务服务，确认 MCP 不自动启动它且状态准确；隔离注册一律恢复/移除并关闭对应客户端会话，旧进程的新调用必须因 stale lease 失败，不能留下指向验收副本的 launcher。失败或仅做临时验收时恢复原计划任务，准备生产切换时则保持写入口关闭并继续第 7 步。
6. 若要记录真实 DeepSeek 验收，必须使用隔离数据目录、用户明确提供的后端凭据和全新客户端会话；分别对 invoice import 与 payment-record attachment 核验 tool description/输入字段正确，并在调用前的可见对话中展示固定 DeepSeek 外发披露，再记录 Codex 风险提示或 Hermes 对该次写调用的批准及实际调用。不能声称 Hermes 原生 prompt 含披露，也不能把 stub 或既往证据写成新的真实调用。
7. 用户选择生产切换时，保持计划任务和客户端写入口关闭，先对未覆盖的旧生产状态做并校验最终一致恢复集，再部署已验收版本并按下述独立命令执行 migration，完成 `/health` 和浏览器只读烟测；随后运行显式注册（路径必须是生产 launcher），关闭/刷新两个客户端并在新会话确认精确 discovery，最后恢复计划任务并记录首个新合同真实写入时间。此前失败整体回退源码/依赖/数据/归档，此后遵循前向修复边界。

## 回滚边界

- 未通过 M1 前不注册 MCP；未通过 M3 前不暴露写工具；因此实现中途不会进入用户客户端。
- schema migration 前必须有经恢复演练的一致恢复集；回滚前停止 MCP、浏览器写入和计划任务，核对幂等账本、文件暂存、cleanup queue 及 `export_operations` 没有 in-progress/unknown 项，不能在未对账时切换代码。
- 新增列/表/索引在结构上是 additive，不代表旧代码理解版本、pending 操作或新状态语义。首次成功 Agent/新合同写入前，可按测试过的代码回退步骤恢复；一旦真实数据经过新写路径，默认只允许前向修复。
- 如用户明确选择真正降级，只能同时恢复匹配恢复集中的旧源码/锁定依赖、完整 `INVOICE_APP_DATA_DIR` 和外部 archive root，并明确接受丢失恢复点之后的全部业务写入；不得只恢复数据库，也不得把“旧代码能打开 additive schema”表述成无损回滚。
- 客户端配置修改使用受 journal 管理的事务临时 unique backup，失败时按 CAS 规则恢复，成功/补偿完成后清理；`-Remove` 可移除 hash 匹配的受管条目。
- 真正降级前先移除/禁用 MCP 注册；恢复点之后的 operation IDs 不得在旧数据库上重放。未来重新启用需重新走能力/数据迁移验收并使用新的 operation IDs。
- MCP 注册与应用后台服务相互独立；移除 MCP 不删除业务数据，卸载后台入口也不静默删除客户端配置。
- 若 Codex 无法执行配置的 writes 风险策略，或 Hermes 逐次写审批 gate 不能 fail closed，保留应用原有浏览器流程并移除该客户端 MCP 条目，Plan 保持 `Active`，不得宣称 Windows 双客户端完成。

## 第二阶段：Apple Silicon macOS

在 Mac 实机上另建 owning Plan。该计划至少覆盖原生 arm64 Python/依赖、安装与卸载、LaunchAgent、数据目录迁移、Finder 权限与 `open` 集成、Codex/Hermes 实际配置路径、睡眠/重启/登录恢复，以及含空格和中文路径。Windows 结果只能证明平台中立模块未直接依赖 Windows API，不能替代 macOS 实机验收。

## 完成证据（2026-08-24）

### 自动化、协议与浏览器

- 隔离副本和生产部署后的全量回归分别通过 `151 passed in 28.52s` 与 `151 passed in 37.08s`；`web/` 下 8 个 JavaScript 文件逐一通过 `node --check`，Python `compileall` 通过。
- MCP SDK 真实客户端分别通过 STDIO 与 HTTP 集成验证；现代 discovery、旧式 initialize 兼容路径、Schema、ToolAnnotations 和 structured output 均通过。allowed roots 为空时工具集精确为 15 项；配置受控 root 时精确为 17 项且只增加两个文件工具，不存在 resources、prompts 或计划外工具。
- 隔离真实浏览器完成手工草稿、编辑、核对确认、重复项 keep-separate、批次、材料和导出闭环，生成 5 页、143429 bytes 的 PDF；两个标签页并发修改时旧提交收到 stale 提示并刷新。全部受影响流程的 Console error/warning 列表为空。
- `DEEPSEEK_API_KEY` 在验收进程中显式置空。两个文件工具均在读取文件前展示固定外发披露并走 `recognition_failed_manual_fallback`；本计划没有发起或声称新的真实 DeepSeek 调用。

### Windows 真实客户端

- Codex CLI `0.149.0`：实际配置和全新会话发现 15/17 项精确工具集；`approval_policy=never` 时写调用被客户端拒绝，交互会话逐次批准后完成导入、核对、确认、附件、批次和导出。相同 `operation_id` 重放返回 `replayed=true`，资源版本和产物不重复变化。隔离验收完成后临时注册与信任状态被移除，Codex 配置按事前字节精确恢复；具体配置指纹不写入公开仓库。
- Hermes Agent `0.20.1`：实际 `chat --cli` 会话发现 15/17 项精确工具集；`trust: untrusted` 对写调用显示 once/session/deny gate，显式 deny 不执行、once 批准后完成同一财务闭环与幂等重放。隔离受管条目和 lease 随后移除。
- 已知客户端限制：Hermes 自身会在真实会话中规范化用户 YAML；受管条目语义已清除，但外部客户端造成的注释/排版变化无法字节级恢复。注册器自身的临时配置、journal、CAS 和故障注入测试仍证明其管理范围内可精确回滚；用户配置大小与指纹不写入公开仓库。

### 一致恢复集与还原演练

- 切换前在项目、生产数据和归档目录之外制作了一致恢复集，保存旧源码、schema v3 数据、ACL、Python 版本/架构、锁定依赖、离线 wheelhouse 和旧计划任务定义；恢复路径、容量和内容指纹不写入公开仓库。
- 恢复集已还原到独立隔离目录；使用锁定旧依赖启动旧版本并通过 reconciliation、`/health`、旧数据/附件只读浏览器烟测，Console 无错误。
- 新依赖另行精确冻结并保存离线 wheelhouse；在全新虚拟环境中仅从该 wheelhouse 安装后，freeze 精确匹配且关键导入成功。

### 生产切换

- 只部署隔离验收过的 52 个源码目标（25 修改、27 新增、0 删除）；`.env.local`、项目 `data/`、`work/` 和 `发票/` 的事前/事后内容守卫一致，具体 manifest 指纹不写入公开仓库。
- 在生产任务停止且 8765 无监听后，以旧数据库指纹为前置条件显式执行 migration：schema v3 升至 v4；以迁移后指纹重跑得到 `status=no_op` 且数据库字节不变。
- 生产前台实例通过 `/health`、SQLite foreign-key check、文件 reconciliation 和真实浏览器旧数据/附件只读烟测，Console 无错误；前台实例停止且端口释放后才进行正式客户端注册和恢复计划任务。
- 正式 Codex/Hermes 注册均指向 `<project-root>\run-agent-mcp.ps1`，allowed roots 为 `[]`，精确暴露 15 项工具；Codex 为 `default_tools_approval_mode=writes`，Hermes 为 `trust=untrusted`。两个全新真实客户端会话均调用生产 `get_service_status` 并得到 `status=running`、`service=invoice-assistant`。
- `InvoiceAssistantBackgroundService` 已恢复为隐藏、非交互 PowerShell 计划任务并处于 Running；最终 `/health` 返回 `ok=true`、`database_check=ok`、0 个外键违规，reconciliation healthy 且 pending/failed cleanup 均为 0。
- 最终生产库为 schema v4，`agent_operations=0`、`file_operation_staging=0`；已有 5 个 `export_operations` 全部为 completed，没有 in-progress/unknown 导出。因此尚未发生首个生产 Agent/新合同真实写入，整体恢复仍处于本 Plan 定义的完整恢复边界。
- 验证期曾发现 Windows lifecycle AST 测试会实际执行隔离 `install.ps1`，从而短暂触碰同名生产计划任务；生产源码、配置和数据守卫确认未改变。测试随后改为纯结构验证并纳入上述 151 项回归。

## 外部兼容性依据

以下动态资料在 2026-08-23 核对；实现开始和最终验收时必须重新检查，不以本文件永久冻结第三方行为：

- [Codex Configuration Reference](https://developers.openai.com/codex/config-reference/)：STDIO `command/cwd`、`enabled_tools`、写审批和 MCP 启动/工具超时。
- [Hermes MCP Configuration Reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/mcp-config-reference.md)：`trust`、tool include、resources/prompts、并行调用和超时。
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)：v2 稳定线及新旧协议兼容行为。
- [MCP ToolAnnotations schema](https://modelcontextprotocol.io/specification/2026-07-28/schema)：read-only、destructive、idempotent 和 open-world 提示语义。
