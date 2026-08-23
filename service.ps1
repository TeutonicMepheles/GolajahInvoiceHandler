param(
    [string]$DataDir
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not [string]::IsNullOrWhiteSpace($DataDir)) {
    if (-not [IO.Path]::IsPathRooted($DataDir)) {
        throw "DataDir must be an absolute path."
    }
    $resolvedDataDir = [IO.Path]::GetFullPath($DataDir)
    $env:INVOICE_APP_DATA_DIR = $resolvedDataDir
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "The runtime is missing. Run install.ps1 first."
}

$env:INVOICE_APP_PORT = "8765"
$env:PYTHONUNBUFFERED = "1"
& $venvPython (Join-Path $PSScriptRoot "run.py")
exit $LASTEXITCODE
