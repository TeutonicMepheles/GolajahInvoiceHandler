# GitHub 首次发布与隐私加固

Status: Active

Last verified: 2026-08-24

## 目标

把当前 Windows 本地发票助手源码发布到名为 `GolajahInvoiceHandler` 的私有 GitHub 仓库，同时确保 Git 历史从第一次提交起就不包含 API Key、真实发票、生产数据库、识别或导出产物、真实金额明细、联系人信息或本机私有配置。

## In scope

- 扩充 `.gitignore`，默认排除环境文件、数据目录、发票目录、工作产物、数据库、日志、PDF/CSV 和本机工具配置。
- 以匿名、无图片、无联系方式、无真实金额和无个人元数据的结构兼容 Word 模板替换原报价模板。
- 移除源码文档中的真实验收金额、票据值、联系人、内部恢复路径和配置/数据指纹。
- 保留明确标注为 synthetic 的测试夹具，使金额计算和导出回归仍可验证。
- 使用显式 allowlist 完成首次暂存，对 staged 内容执行秘密、PII、路径、二进制和敏感目录门禁。
- 建立私有 GitHub 仓库、推送 `main`，并验证远端可见性、默认分支和提交 SHA。

## Out of scope

- 公开仓库可见性；首次发布保持 private。
- 上传生产数据库、恢复集、运行日志、发票文件、验收导出或浏览器产物。
- 在 GitHub 保存 API Key、创建新的 API Key，或改变后端凭据的运行时来源。
- 改变报价计算规则、发票业务合同或生产数据。

## Acceptance gates

- `git ls-files` 只包含批准的源码、测试、匿名模板和文档；禁止 `.env`、`data/`、`work/`、`发票/`、`.claude/`、SQLite、PDF、CSV 和临时产物。
- 对 staged blob 的高可信凭证扫描为 0；`.env.example` 中 API Key 为空。
- 匿名 DOCX 包内 `word/media/` 为 0，正文无邮箱/电话/地址/真实组织或金额，core properties 无 creator/lastModifiedBy；模板和合成导出均完成逐页版面检查。
- 报价专项测试、全量 pytest、所有 `web/*.js` 的 `node --check` 均通过。
- GitHub 仓库 `GolajahInvoiceHandler` 为 private，远端 `main` 与本地 HEAD 一致，远端文件树重新通过敏感路径和秘密门禁。

## 回滚边界

- GitHub 仓库创建前没有远端状态；本地 Git 初始化可直接删除而不影响应用运行或生产数据。
- 首次 push 前若任一门禁失败，不创建或不推送远端。
- 首次 push 后若远端验证失败，立即停止后续提交；由于首次提交本身必须通过门禁，不能依赖“后续删除敏感文件”修复历史泄露。
