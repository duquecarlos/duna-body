# Registers two Windows scheduled tasks. Run once yourself, after signing in.
#   "Body app daily sync"   every morning: pull yesterday + today from Garmin, update readiness, and -- when
#                           -ShiftsDir is given -- import any new shift files from that folder
#   "Body app weekly file"  Sundays 18:00: pull, then write this week's file for the Personal Sunday review (19:00)
# Remove with: Unregister-ScheduledTask -TaskName "Body app daily sync"; Unregister-ScheduledTask -TaskName "Body app weekly file"
# Example with shift import on: .\install-daily-sync.ps1 -ShiftsDir "C:\path\to\your\shifts\folder"
param([string]$At = "09:30", [string]$SundayAt = "18:00", [string]$ShiftsDir)
$repo = Split-Path $PSScriptRoot -Parent
$py = "$repo\.venv\Scripts\python.exe"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable

$dailyActions = @(New-ScheduledTaskAction -Execute $py -Argument "-m body.pull --days 2" -WorkingDirectory $repo)
if ($ShiftsDir) {
    $dailyActions += New-ScheduledTaskAction -Execute $py -Argument "-m body.shift_import --dir `"$ShiftsDir`"" -WorkingDirectory $repo
}
Register-ScheduledTask -TaskName "Body app daily sync" -Action $dailyActions -Trigger (New-ScheduledTaskTrigger -Daily -At $At) -Settings $settings -Description "Pulls Garmin data into the local Body app database" -Force | Out-Null

$pull = New-ScheduledTaskAction -Execute $py -Argument "-m body.pull --days 2" -WorkingDirectory $repo
$file = New-ScheduledTaskAction -Execute $py -Argument "-m body.vida_export --current" -WorkingDirectory $repo
Register-ScheduledTask -TaskName "Body app weekly file" -Action @($pull, $file) -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $SundayAt) -Settings $settings -Description "Writes Body's weekly vida-v1 file for the Personal Sunday review" -Force | Out-Null

$shiftMsg = if ($ShiftsDir) { "on, from $ShiftsDir" } else { "off" }
Write-Host "Registered: daily sync at $At (shift import: $shiftMsg), weekly file on Sundays at $SundayAt (both run later if the PC was off)."
