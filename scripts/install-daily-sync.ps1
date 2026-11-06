# Registers two Windows scheduled tasks. Run once yourself, after signing in.
#   "Body app daily sync"   every morning: pull yesterday + today from Garmin, update readiness
#   "Body app weekly file"  Sundays 18:00: pull, then write this week's file for the Personal Sunday review (19:00)
# Remove with: Unregister-ScheduledTask -TaskName "Body app daily sync"; Unregister-ScheduledTask -TaskName "Body app weekly file"
param([string]$At = "09:30", [string]$SundayAt = "18:00")
$repo = Split-Path $PSScriptRoot -Parent
$py = "$repo\.venv\Scripts\python.exe"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable

$daily = New-ScheduledTaskAction -Execute $py -Argument "-m body.pull --days 2" -WorkingDirectory $repo
Register-ScheduledTask -TaskName "Body app daily sync" -Action $daily -Trigger (New-ScheduledTaskTrigger -Daily -At $At) -Settings $settings -Description "Pulls Garmin data into the local Body app database" -Force | Out-Null

$pull = New-ScheduledTaskAction -Execute $py -Argument "-m body.pull --days 2" -WorkingDirectory $repo
$file = New-ScheduledTaskAction -Execute $py -Argument "-m body.vida_export --current" -WorkingDirectory $repo
Register-ScheduledTask -TaskName "Body app weekly file" -Action @($pull, $file) -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $SundayAt) -Settings $settings -Description "Writes Body's weekly vida-v1 file for the Personal Sunday review" -Force | Out-Null

Write-Host "Registered: daily sync at $At, weekly file on Sundays at $SundayAt (both run later if the PC was off)."
