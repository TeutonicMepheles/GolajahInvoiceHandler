$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$appUrl = "http://127.0.0.1:8765/"
$healthUrl = "http://127.0.0.1:8765/health"
$taskName = "InvoiceAssistantBackgroundService"
$serviceScript = Join-Path $PSScriptRoot "service.ps1"
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$lifecycleScript = Join-Path $PSScriptRoot "install_lifecycle.py"

function Get-TaskDataRoot($task) {
    if (-not $task) {
        return $null
    }
    foreach ($action in @($task.Actions)) {
        $match = [regex]::Match([string]$action.Arguments, '(?:^|\s)-DataDir\s+"([^"]+)"')
        if ($match.Success) {
            return [IO.Path]::GetFullPath($match.Groups[1].Value)
        }
    }
    return $null
}

function Resolve-RuntimeDataRoot {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "The runtime is missing. Run install.ps1 first."
    }
    $runtimeConfigJson = & $venvPython $lifecycleScript resolve-config --project-root $PSScriptRoot
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$runtimeConfigJson)) {
        throw "Failed to resolve the configured data root."
    }
    $runtimeConfig = $runtimeConfigJson | ConvertFrom-Json
    return [IO.Path]::GetFullPath([string]$runtimeConfig.data_dir)
}

function Test-InvoiceAssistantReady {
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        return $health.ok -eq $true -and $health.service -eq "invoice-assistant"
    }
    catch {
        return $false
    }
}

if (-not (Test-InvoiceAssistantReady)) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $activeDataRoot = Get-TaskDataRoot $task
    if ($task) {
        if ($task.State -ne "Running") {
            Start-ScheduledTask -TaskName $taskName
        }
    }
    else {
        $activeDataRoot = Resolve-RuntimeDataRoot
        Start-Process -FilePath $powershell `
            -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$serviceScript`" -DataDir `"$activeDataRoot`"" `
            -WindowStyle Hidden
    }

    $ready = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-InvoiceAssistantReady) {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        if (-not $activeDataRoot) {
            $activeDataRoot = Resolve-RuntimeDataRoot
        }
        $logPath = Join-Path $activeDataRoot "logs\service.log"
        throw "Invoice Assistant failed to start. See the service log: $logPath"
    }
}

Start-Process $appUrl
