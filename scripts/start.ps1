# Start Laya (if it is not already running) and the dashboard on http://127.0.0.1:8100.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\laya.ps1"

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Project virtual environment not found. From $root run:`n  py -3.14 -m venv .venv`n  .venv\Scripts\python.exe -m pip install -r requirements.txt"
}

if (-not (Start-LayaIfNeeded)) {
    Write-Warning "Continuing without Laya: new opportunities are saved as pending_evaluation."
}

Set-Location $root
Write-Host "Dashboard: http://127.0.0.1:8100   (Ctrl+C to stop)"
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8100
