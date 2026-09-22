# Windows Release 与本地源码整合

Status: Implemented

Last verified: 2026-09-22

## 目标与范围

保留已有 GitHub main 历史，按源码白名单整合本地最新文件，发布可供 Windows 用户解压安装的 ZIP、SHA-256 校验文件和部署说明。新增 CMD 安装入口调用既有安装脚本，不改变业务或数据库合同。

## Out of scope

- 上传本地业务数据、环境配置、密钥、日志、虚拟环境或验收产物。
- 更改仓库私有可见性、升级生产数据库、停止本机生产服务。
- 打包独立 EXE 或离线 Python 运行环境；放宽 Agent 客户端版本保护。

## Acceptance gates

- 发布候选仅包含审计后的源码、文档、合成测试和匿名模板；秘密扫描与 DOCX 内部检查通过。
- 全量 pytest 与所有前端 JS 语法检查实际执行；已知环境相关失败明确记录。
- ZIP 从审计后的 Git 提交生成，条目及内容再次核对，无工作区或业务数据混入。
- main 推送成功，Release 标签指向同一提交，ZIP 和 SHA256SUMS 上传并验证。

## 验证记录

- 本地全量回归：173 passed、1 failed。失败项为真实客户端注册门禁，本机 Codex 0.153.4 超过已验证的 0.149.0；没有放宽保护，部署说明已记录限制。
- 所有 10 个前端 JavaScript 文件通过 node --check；所有根目录 PowerShell 脚本通过语法解析。
- 干净候选共 104 个文件；凭据、私有配置值、个人路径、邮箱和手机号扫描无命中。DOCX 无媒体、creator 或 lastModifiedBy。
- 候选代码使用隔离空数据目录显式迁移并启动临时 HTTP 服务，/health、/、/api/bootstrap、/static/app.js 均返回 200；未触碰生产数据或停止生产服务。
- 新增部署文档与 CMD 包装入口，业务代码与原远端版本仅空行差异；保留本地最新源码。
- 未进行全新 Windows 机器安装、登录自启、真实浏览器或外部识别服务验收。Release 为联网安装的源码部署 ZIP，不含 Python 或离线依赖。
- 发布提交、标签和附件校验值由 GitHub Release 页面与 SHA256SUMS.txt 记录。
