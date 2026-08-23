$ErrorActionPreference = "Stop"

$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$serverModule = Join-Path $projectRoot "invoice_assistant\features\agent_mcp\server.py"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    [Console]::Error.WriteLine("Invoice MCP launcher: virtual-environment Python is missing: $venvPython")
    exit 1
}
if (-not (Test-Path -LiteralPath $serverModule -PathType Leaf)) {
    [Console]::Error.WriteLine("Invoice MCP launcher: server module is missing: $serverModule")
    exit 1
}

try {
    Set-Location -LiteralPath $projectRoot
    & $venvPython -m invoice_assistant.features.agent_mcp.server
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        $exitCode = 1
    }
    exit $exitCode
}
catch {
    [Console]::Error.WriteLine("Invoice MCP launcher failed: $($_.Exception.Message)")
    exit 1
}
