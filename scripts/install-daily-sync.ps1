# Registers a Windows scheduled task that pulls yesterday + today every morning.
# Run once, after signing in. Remove with: Unregister-ScheduledTask -TaskName "Body app daily sync"
param([string]$At = "09:30")
$repo = Split-Path $PSScriptRoot -Parent
$action = New-ScheduledTaskAction -Execute "$repo\.venv\Scripts\python.exe" -Argument "-m body.pull --days 2" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable
Register-ScheduledTask -TaskName "Body app daily sync" -Action $action -Trigger $trigger -Settings $settings -Description "Pulls Garmin data into the local Body app database" -Force | Out-Null
Write-Host "Daily sync registered at $At (runs later if the PC was off)."
