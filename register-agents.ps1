param(
    [ValidateSet("all", "codex", "hermes")]
    [string]$Agent = "all",

    [string[]]$AllowedRoot = @(),

    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA "InvoiceAssistant\agent-registration"),

    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$registrationModule = Join-Path $projectRoot "invoice_assistant\features\agent_mcp\registration.py"
$launcher = Join-Path $projectRoot "run-agent-mcp.ps1"
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    [Console]::Error.WriteLine("Agent registration: virtual-environment Python is missing: $venvPython")
    exit 40
}
if (-not (Test-Path -LiteralPath $registrationModule -PathType Leaf)) {
    [Console]::Error.WriteLine("Agent registration: registration module is missing: $registrationModule")
    exit 40
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    [Console]::Error.WriteLine("Agent registration: MCP launcher is missing: $launcher")
    exit 40
}
if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) {
    [Console]::Error.WriteLine("Agent registration: Windows PowerShell is missing: $powershell")
    exit 21
}

$arguments = @(
    "-m", "invoice_assistant.features.agent_mcp.registration",
    "--agent", $Agent,
    "--state-root", $StateRoot,
    "--project-root", $projectRoot,
    "--powershell-path", $powershell,
    "--launcher-path", $launcher
)
foreach ($root in $AllowedRoot) {
    $arguments += @("--allowed-root", $root)
}
if ($Remove) {
    $arguments += "--remove"
}

$locationPushed = $false
try {
    # `python -m` resolves the local package from the project root.  Keep the
    # registration entry independent of the caller's current directory while
    # restoring that directory when the script is dot-sourced.
    Push-Location -LiteralPath $projectRoot
    $locationPushed = $true
    & $venvPython @arguments
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        $exitCode = 40
    }
}
catch {
    [Console]::Error.WriteLine("Agent registration failed to start: $($_.Exception.Message)")
    $exitCode = 40
}
finally {
    if ($locationPushed) {
        Pop-Location
    }
}
exit $exitCode
