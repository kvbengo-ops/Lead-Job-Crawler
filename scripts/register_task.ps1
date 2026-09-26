# Register (or remove) the Windows scheduled task that runs run_crawl.ps1 several times a day.
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Times 07:30,12:00,17:00,21:00
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Unregister
# The task runs only while you are logged on (no password is stored), catches up on runs missed while
# the PC was off or asleep, never runs two copies at once, and is stopped after 2 hours.
param(
    [string[]]$Times = @("08:00", "13:00", "18:00"),
    [string]$TaskName = "JobCrawler",
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    exit 0
}

if ($Times.Count -lt 1) { throw "Give at least one time, for example -Times 08:00,13:00,18:00" }
$triggers = foreach ($t in $Times) {
    try { $at = [datetime]::ParseExact($t.Trim(), "HH:mm", [Globalization.CultureInfo]::InvariantCulture) }
    catch { throw "Invalid time '$t'. Use 24-hour HH:mm, for example 08:00 or 18:30." }
    New-ScheduledTaskTrigger -Daily -At $at
}

$root = Split-Path $PSScriptRoot -Parent
$runner = Join-Path $PSScriptRoot "run_crawl.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $root `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal -Description "Local Job and Lead Crawler: runs scripts\run_crawl.ps1" -Force | Out-Null

$sorted = $Times | ForEach-Object { $_.Trim() } | Sort-Object
Write-Host "Registered scheduled task '$TaskName': daily at $($sorted -join ', ')."
Write-Host "Logs: $(Join-Path $root 'data\logs')   Remove with: scripts\register_task.ps1 -Unregister"
