$ErrorActionPreference = "Stop"

$registrationScript = Join-Path $PSScriptRoot "register-agents.ps1"
$defaultRegistrationState = Join-Path $env:LOCALAPPDATA "InvoiceAssistant\agent-registration\managed-state.json"
if (Test-Path -LiteralPath $defaultRegistrationState -PathType Leaf) {
    Write-Warning "Managed Codex/Hermes registration still exists. Before uninstalling, run: & `"$registrationScript`" -Agent all -Remove"
    Write-Warning "This uninstaller will not edit external client configuration. If a custom -StateRoot was used, remove it with that same StateRoot."
}

$taskName = "InvoiceAssistantBackgroundService"
$displayName = "$([char]0x53D1)$([char]0x7968)$([char]0x62A5)$([char]0x9500)$([char]0x7BA1)$([char]0x7406)$([char]0x52A9)$([char]0x624B)"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $taskName
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$shortcuts = @(
    (Join-Path ([Environment]::GetFolderPath("DesktopDirectory")) "$displayName.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "$displayName\$displayName.lnk")
)
foreach ($shortcut in $shortcuts) {
    Remove-Item -LiteralPath $shortcut -Force -ErrorAction SilentlyContinue
}

Write-Output "The background entry was removed. Persistent data was not deleted."
