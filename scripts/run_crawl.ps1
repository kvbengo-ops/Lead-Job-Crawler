# Run one crawl: make sure Laya is up, run `python -m app.crawl`, log to data\logs\crawl-YYYY-MM-DD.log.
# Used by the scheduled task (see register_task.ps1); can also be run by hand:
#   powershell -ExecutionPolicy Bypass -File scripts\run_crawl.ps1
# If this script had to start Laya, it stops it again afterwards to free GPU memory and RAM.
# Pass -KeepLaya to leave it running.
param([switch]$KeepLaya)

$root = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\laya.ps1"

$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("crawl-{0:yyyy-MM-dd}.log" -f (Get-Date))
function Write-Log([string]$message) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $message
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Log "ERROR: project virtual environment not found at $python"
    exit 2
}

Write-Log "=== crawl run started"
if (-not (Start-LayaIfNeeded -Hidden -TimeoutSeconds 300)) {
    Write-Log "WARNING: Laya is not available; new items will be saved as pending_evaluation"
}

# app.crawl reads sources.json, profile.json and data\ relative to the project root.
Set-Location $root
# Python must write UTF-8 when its output is piped, or a non-ASCII title crashes print().
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$exitCode = 1
try {
    # Stderr lines from a native program become error records in Windows PowerShell; keep them as log text.
    $ErrorActionPreference = "Continue"
    & $python -m app.crawl 2>&1 | ForEach-Object { Write-Log "$_" }
    $exitCode = $LASTEXITCODE
} finally {
    if (-not $KeepLaya) { Stop-LayaIfStarted }
    Write-Log "=== crawl run finished with exit code $exitCode"
}
exit $exitCode
