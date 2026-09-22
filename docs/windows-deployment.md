# Windows 部署

## 安装

1. 使用 Windows 10/11，安装 64 位 Python 3.12（本次验证版本为 3.12.14），安装时选中 **Add python.exe to PATH**。在 PowerShell 执行 `python --version` 确认可用。
2. 下载 Release 中的 `GolajahInvoiceHandler-*-windows.zip`，完整解压到当前用户可写、计划长期保留的目录。不要在压缩包内直接运行，也不要与其他版本混合覆盖。
3. 双击根目录的 `Install-Windows.cmd`。安装需要联网下载 Python 依赖；发布包不包含 Python、虚拟环境或离线依赖。
4. 安装完成后浏览器打开 `http://127.0.0.1:8765/`，以后使用桌面或开始菜单的“发票报销管理助手”入口。安装会创建当前用户登录后运行的后台计划任务。

也可在解压目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

本应用仅监听本机地址，不需要 Node.js、Git 或 Codex。若 Python 不可用，重新打开终端并检查 PATH；若端口 8765 已占用，先确认并停止占用该端口的旧应用，再安装。

## 可选识别配置

将根目录 `.env.example` 复制为 `.env.local`，在本机填写自己的 `DEEPSEEK_API_KEY`。不要分享这个文件。未配置密钥仍可手工录入与整理材料；配置识别后，发票或支付文件会发送到 DeepSeek。配置更新后重启后台任务。

## 数据和升级

业务数据默认保存于 `%LOCALAPPDATA%\InvoiceAssistant\data\`，与解压目录分离。发布包不包含任何用户数据库、发票、附件、归档、日志或识别凭据。

升级前停止后台任务和所有手工/Agent 写入口，备份完整数据目录、旧程序与私有配置，并验证可以恢复。把新版本解压到独立目录，按根目录 README 的升级流程操作。安装发现旧数据库结构时会停止并提示显式迁移命令，不会自动升级业务数据。不要为了绕过迁移提示而删除数据。

项目路径变化后，重新运行安装以刷新快捷方式与计划任务。卸载入口可运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1`，不会删除业务数据。

## 可选 Agent 接入限制

网页应用无需 Agent。`register-agents.ps1` 对客户端版本有明确的验证门槛：当前代码支持 Codex 0.149.0、Hermes 0.20.1。其他版本会被拒绝；请勿绕过保护。本次本机 Codex 0.153.4 的真实客户端注册测试因此失败，其余 173 项回归通过。尚未在全新 Windows 机器上完成安装验收。

## 校验下载

Release 同时提供 `SHA256SUMS.txt`。下载后执行并与其中记录比对：

```powershell
Get-FileHash .\GolajahInvoiceHandler-*-windows.zip -Algorithm SHA256
```

仓库当前为私有，下载者需要仓库访问权限；也可以直接向他人提供已下载的干净 ZIP。
