# Registers three Windows scheduled tasks. Run once yourself, after signing in.
#   "Body app daily sync"   every morning: pull yesterday + today from Garmin, update readiness, and -- when
#                           -ShiftsDir is given -- import any new shift files from that folder
#   "Body app weekly file"  Sundays 18:00: pull, then write this week's file for the Personal Sunday review (19:00)
#   "Body app web"          at logon: runs the web app (local only by default; add -Lan to also open it on the
#                           home network) and restarts it if it stops
# Remove with: Unregister-ScheduledTask -TaskName "Body app daily sync"; Unregister-ScheduledTask -TaskName "Body app weekly file"; Unregister-ScheduledTask -TaskName "Body app web"
# Example with shift import on: .\install-daily-sync.ps1 -ShiftsDir "C:\path\to\your\shifts\folder"
# Example with the web app open to the home network too: .\install-daily-sync.ps1 -Lan
param([string]$At = "09:30", [string]$SundayAt = "18:00", [string]$ShiftsDir, [int]$Port = 8770, [switch]$Lan)
$repo = Split-Path $PSScriptRoot -Parent
$py = "$repo\.venv\Scripts\python.exe"
$pyw = "$repo\.venv\Scripts\pythonw.exe"
$webPy = if (Test-Path $pyw) { $pyw } else { $py }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable

$dailyActions = @(New-ScheduledTaskAction -Execute $py -Argument "-m body.pull --days 2" -WorkingDirectory $repo)
if ($ShiftsDir) {
    $dailyActions += New-ScheduledTaskAction -Execute $py -Argument "-m body.shift_import --dir `"$ShiftsDir`"" -WorkingDirectory $repo
}
Register-ScheduledTask -TaskName "Body app daily sync" -Action $dailyActions -Trigger (New-ScheduledTaskTrigger -Daily -At $At) -Settings $settings -Description "Pulls Garmin data into the local Body app database" -Force | Out-Null

$pull = New-ScheduledTaskAction -Execute $py -Argument "-m body.pull --days 2" -WorkingDirectory $repo
$file = New-ScheduledTaskAction -Execute $py -Argument "-m body.vida_export --current" -WorkingDirectory $repo
Register-ScheduledTask -TaskName "Body app weekly file" -Action @($pull, $file) -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $SundayAt) -Settings $settings -Description "Writes Body's weekly vida-v1 file for the Personal Sunday review" -Force | Out-Null

$webArgs = "-m body.web --port $Port"
if ($Lan) { $webArgs += " --lan" }
$webAction = New-ScheduledTaskAction -Execute $webPy -Argument $webArgs -WorkingDirectory $repo
$webTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$webSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Body app web" -Action $webAction -Trigger $webTrigger -Settings $webSettings -Description "Runs the Body app web server at logon, restarting it if it stops" -Force | Out-Null
Stop-ScheduledTask -TaskName "Body app web" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "Body app web"

$shiftMsg = if ($ShiftsDir) { "on, from $ShiftsDir" } else { "off" }
$webMsg = if ($Lan) {
    "Body app web running at http://127.0.0.1:$Port -- and open to every device on this home network."
} else {
    "Body app web running at http://127.0.0.1:$Port."
}
Write-Host "Registered: daily sync at $At (shift import: $shiftMsg), weekly file on Sundays at $SundayAt (both run later if the PC was off). $webMsg"
