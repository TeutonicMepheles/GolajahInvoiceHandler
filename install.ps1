param(
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$taskName = "InvoiceAssistantBackgroundService"
$displayName = "$([char]0x53D1)$([char]0x7968)$([char]0x62A5)$([char]0x9500)$([char]0x7BA1)$([char]0x7406)$([char]0x52A9)$([char]0x624B)"
$appUrl = "http://127.0.0.1:8765/"
$healthUrl = "http://127.0.0.1:8765/health"
$serviceScript = Join-Path $PSScriptRoot "service.ps1"
$launcherScript = Join-Path $PSScriptRoot "start.ps1"
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask -and $existingTask.State -eq "Running") {
    Stop-ScheduledTask -TaskName $taskName
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if (-not $existingTask -or $existingTask.State -ne "Running") {
            break
        }
    }
    if ($existingTask -and $existingTask.State -eq "Running") {
        throw "The existing background task did not stop; runtime and data were not upgraded."
    }
}
$fixedPortListener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($fixedPortListener) {
    throw "Port 8765 is still in use. Stop every Invoice Assistant/manual write entry before installation."
}

$bootstrapPython = $venvPython
if (-not (Test-Path -LiteralPath $bootstrapPython -PathType Leaf)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python was not found. Install Python 3.12 or newer first."
    }
    $bootstrapPython = $pythonCommand.Source
}

$lifecycleScript = Join-Path $PSScriptRoot "install_lifecycle.py"
$runtimeConfigJson = & $bootstrapPython $lifecycleScript resolve-config --project-root $PSScriptRoot
if ($LASTEXITCODE -ne 0) {
    throw "Failed to resolve the fixed-port runtime configuration."
}
$runtimeConfig = $runtimeConfigJson | ConvertFrom-Json
$dataRoot = [IO.Path]::GetFullPath([string]$runtimeConfig.data_dir)
$database = Join-Path $dataRoot "invoice_assistant.sqlite3"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    & $bootstrapPython -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the Python virtual environment."
    }
}

& $venvPython -m pip install --disable-pip-version-check --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install the runtime dependencies."
}

$databaseStateJson = & $venvPython $lifecycleScript inspect-data-root --data-dir $dataRoot
if ($LASTEXITCODE -eq 3) {
    throw "The configured data root is in use; installation did not register or start the service: $dataRoot"
}
if ($LASTEXITCODE -ne 0) {
    throw "Failed to inspect the configured data root; installation did not register or start the service: $dataRoot"
}
$databaseState = $databaseStateJson | ConvertFrom-Json

if ($databaseState.state -eq "migration_required") {
    $databaseHash = [string]$databaseState.database_sha256
    $migrationDetail = [string]$databaseState.detail
    throw "Existing data requires an explicit, recovery-tested migration ($migrationDetail). Keep the service stopped, create and verify a consistent recovery set, then run: & `"$venvPython`" -m invoice_assistant.migrate --data-dir `"$dataRoot`" --expect-db-sha256 $databaseHash"
}
elseif ($databaseState.state -eq "current") {
    # The complete current schema, integrity and logical SQLite-state hash were
    # checked while holding the same cross-session data-root mutex as the service.
}
elseif ($databaseState.state -eq "strictly_empty") {
    & $venvPython -m invoice_assistant.migrate --data-dir $dataRoot --expect-no-database
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to initialize the strictly empty data root."
    }
}
elseif ($databaseState.state -eq "nonempty_without_database") {
    throw "The configured data root is non-empty but has no database. Move or reconcile its contents before installation: $dataRoot"
}
elseif ($databaseState.state -eq "invalid_database_path") {
    throw "The configured database path exists but is not a file: $database"
}
else {
    throw "The configured data root exists but is not a directory: $dataRoot"
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$serviceArguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$serviceScript`" -DataDir `"$dataRoot`""
$action = New-ScheduledTaskAction -Execute $powershell -Argument $serviceArguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Runs Invoice Assistant after user logon and restarts it after failures."
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null

function New-AppShortcut([string]$shortcutPath) {
    $parent = Split-Path -Parent $shortcutPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $powershell
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcherScript`""
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.Description = "Open Invoice Assistant"
    $shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,220"
    $shortcut.Save()
}

$desktopShortcut = Join-Path ([Environment]::GetFolderPath("DesktopDirectory")) "$displayName.lnk"
$startMenuShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "$displayName\$displayName.lnk"
New-AppShortcut $desktopShortcut
New-AppShortcut $startMenuShortcut

Start-ScheduledTask -TaskName $taskName
$ready = $false
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($health.ok -eq $true -and $health.service -eq "invoice-assistant") {
            $ready = $true
            break
        }
    }
    catch {
        # The service can take a few seconds to initialize after first installation.
    }
}
if (-not $ready) {
    $logPath = Join-Path $dataRoot "logs\service.log"
    throw "The background service did not start. See the service log: $logPath"
}

Write-Output "$displayName is installed and will start after user logon."
Write-Output "Stable entry: $appUrl"
if (-not $NoOpen) {
    Start-Process $appUrl
}
