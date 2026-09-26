# Shared Laya helpers. Dot-source it:  . "$PSScriptRoot\laya.ps1"
# Override with environment variables: LAYA_PYTHON, LAYA_URL, LAYA_DEVICE, LAYA_MODELS.

$LayaPython = if ($env:LAYA_PYTHON) { $env:LAYA_PYTHON } else { "$env:USERPROFILE\Desktop\Laya\.venv\Scripts\python.exe" }
# 127.0.0.1, not localhost: Windows tries IPv6 first and Laya listens on IPv4 only.
$LayaUrl = if ($env:LAYA_URL) { $env:LAYA_URL.TrimEnd("/") } else { "http://127.0.0.1:8000" }

function Test-Laya {
    try { (Invoke-RestMethod "$LayaUrl/health" -TimeoutSec 3).status -eq "ok" } catch { $false }
}

# Set when Start-LayaIfNeeded launches Laya, so the caller can stop it again with Stop-LayaIfStarted.
$LayaProcess = $null

function Start-LayaIfNeeded {
    param([int]$TimeoutSeconds = 300, [switch]$Hidden)
    if (Test-Laya) { Write-Host "Laya is already running at $LayaUrl"; return $true }
    if (-not (Test-Path $LayaPython)) {
        Write-Warning "Laya not found at $LayaPython. Set LAYA_PYTHON to its python.exe."
        return $false
    }
    $device = if ($env:LAYA_DEVICE) { $env:LAYA_DEVICE } else { "cuda" }
    $models = if ($env:LAYA_MODELS) { $env:LAYA_MODELS } else { "english" }
    $port = ([uri]$LayaUrl).Port
    $py = $LayaPython.Replace("'", "''")
    # -NoExit keeps the window open if Laya crashes, so the error stays readable.
    $command = "`$host.UI.RawUI.WindowTitle = 'Laya server'; `$env:LAYA_DEVICE = '$device'; " +
               "`$env:LAYA_MODELS = '$models'; `$env:LAYA_PORT = '$port'; & '$py' -m laya.serve"
    $style = if ($Hidden) { "Hidden" } else { "Minimized" }
    Write-Host "Starting Laya ($device, $models) on port $port in a $($style.ToLower()) window titled 'Laya server'..."
    $script:LayaProcess = Start-Process powershell -ArgumentList "-NoExit", "-NoProfile", "-Command", $command -WindowStyle $style -PassThru
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Laya) { Write-Host "Laya is up."; return $true }
        Start-Sleep -Seconds 2
    }
    Write-Warning "Laya did not answer at $LayaUrl within $TimeoutSeconds seconds."
    return $false
}

function Stop-LayaIfStarted {
    # Only stops a Laya that Start-LayaIfNeeded launched in this session; one you started yourself is left alone.
    if ($script:LayaProcess -and -not $script:LayaProcess.HasExited) {
        taskkill /PID $script:LayaProcess.Id /T /F | Out-Null
        Write-Host "Stopped the Laya server this script started."
    }
    $script:LayaProcess = $null
}
