# Registers four Windows scheduled tasks. Run once yourself, after signing in.
#   "Body app daily sync"    every morning: pull yesterday + today from Garmin, update readiness, and -- when
#                             -ShiftsDir is given -- import any new shift files from that folder
#   "Body app weekly file"   Sundays 18:00: pull, then write this week's file for the Personal Sunday review (19:00)
#   "Body app morning file"  -MorningAt times (default 04:40, 07:40, 12:40): pull today, then write today's
#                             vida-v1 daily file for Personal's morning brief -- each run rewrites the same file
#   "Body app wake check"    04:00, repeating every 20 minutes for 9 hours: a light sleep-only Garmin check
#                             (BODY-DESPERTAR-1) that writes BODY_HOME/vida/despertar-<day>.json as soon as last
#                             night's wake time is known, then calls no more Garmin for that day
#   "Body app web"           at logon: runs the web app (local only by default; add -Lan to also open it on the
#                             home network) and restarts it if it stops
# Every task action runs through `python -m body.vivo <task>` from a SIBLING git worktree (-LiveDir,
# default "$repo-live") detached at `main`, refreshed at the start of every run (BODY-LIVE-CHECKOUT-1) --
# so "restart the web task" always serves `main`, even while this repo's own working tree sits on a
# branch mid-slice. The venv stays the one at $repo\.venv; only the code tree differs.
# Remove with: Unregister-ScheduledTask -TaskName "Body app daily sync"; Unregister-ScheduledTask -TaskName "Body app weekly file"; Unregister-ScheduledTask -TaskName "Body app morning file"; Unregister-ScheduledTask -TaskName "Body app wake check"; Unregister-ScheduledTask -TaskName "Body app web"; git -C <repo> worktree remove <LiveDir>
# Example with shift import on: .\install-daily-sync.ps1 -ShiftsDir "C:\path\to\your\shifts\folder"
# Example with the web app open to the home network too: .\install-daily-sync.ps1 -Lan
#
# -Hosted: the host (body.duna.solutions) does the pull and runs the web app instead of this PC.
# Link this PC first with `python -m body.conectar_pc`, then run with -Hosted (-ShiftsDir required):
#   "Body app shift upload"  daily: uploads new shift files to the hosted Body
#   "Body app weekly fetch"  Sundays: fetches this week's Body section for the Personal Sunday review
# -Hosted also removes "Body app daily sync", "Body app weekly file" and "Body app web" if present.
# Example, hosted: .\install-daily-sync.ps1 -Hosted -ShiftsDir "C:\path\to\your\shifts\folder"
param([string]$At, [string]$SundayAt = "18:00", [string[]]$MorningAt = @("04:40", "07:40", "12:40"), [string]$ShiftsDir, [int]$Port = 8770, [switch]$Lan, [switch]$Hosted, [string]$LiveDir)
$repo = Split-Path $PSScriptRoot -Parent
if (-not $LiveDir) { $LiveDir = "$repo-live" }
$py = "$repo\.venv\Scripts\python.exe"
$pyw = "$repo\.venv\Scripts\pythonw.exe"
$webPy = if (Test-Path $pyw) { $pyw } else { $py }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable

$repoGitDir = (Resolve-Path -LiteralPath "$repo\.git").Path
if (Test-Path -LiteralPath $LiveDir) {
    $liveIsWorktreeOfRepo = $false
    $liveCommonDirRaw = & git -C $LiveDir rev-parse --git-common-dir 2>$null
    if ($LASTEXITCODE -eq 0 -and $liveCommonDirRaw) {
        $liveCommonDirRaw = $liveCommonDirRaw.Trim()
        if (-not [System.IO.Path]::IsPathRooted($liveCommonDirRaw)) {
            $liveCommonDirRaw = Join-Path (Resolve-Path -LiteralPath $LiveDir).Path $liveCommonDirRaw
        }
        $liveCommonDirFull = (Resolve-Path -LiteralPath $liveCommonDirRaw -ErrorAction SilentlyContinue)
        if ($liveCommonDirFull) {
            $liveIsWorktreeOfRepo = $liveCommonDirFull.Path -ieq $repoGitDir
        }
    }
    if (-not $liveIsWorktreeOfRepo) {
        Write-Host "$LiveDir ya existe y no es un arbol vivo de $repo. No lo toco; borralo o elige otro -LiveDir."
        exit 1
    }
} else {
    $hasMain = & git -C $repo rev-parse --verify --quiet main
    if (-not $hasMain) {
        Write-Host "El repo en $repo no tiene una rama 'main'. No puedo crear el arbol vivo en $LiveDir."
        exit 1
    }
    git -C $repo worktree add --detach $LiveDir main
    if ($LASTEXITCODE -ne 0) {
        Write-Host "No pude crear el arbol vivo en $LiveDir (git worktree add --detach)."
        exit 1
    }
}

if ($Hosted) {
    $bodyHome = if ($env:BODY_HOME) { $env:BODY_HOME } else { "$env:USERPROFILE\.body-app" }
    if (-not (Test-Path "$bodyHome\dispositivo.json")) {
        Write-Host "Este PC no esta conectado todavia. Corre primero: python -m body.conectar_pc"
        exit 1
    }
    if (-not $ShiftsDir) {
        Write-Host "-Hosted necesita -ShiftsDir (la carpeta donde llegan los archivos turnos-*.json)."
        exit 1
    }
    if (-not $At) { $At = "09:15" }

    $removed = @()
    foreach ($name in @("Body app daily sync", "Body app weekly file", "Body app web")) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            $removed += $name
        }
    }

    $uploadAction = New-ScheduledTaskAction -Execute $py -Argument "-m body.vivo subir_turnos --dir `"$ShiftsDir`"" -WorkingDirectory $LiveDir
    Register-ScheduledTask -TaskName "Body app shift upload" -Action $uploadAction -Trigger (New-ScheduledTaskTrigger -Daily -At $At) -Settings $settings -Description "Uploads new shift files to the hosted Body" -Force | Out-Null

    $fetchAction = New-ScheduledTaskAction -Execute $py -Argument "-m body.vivo traer_semana" -WorkingDirectory $LiveDir
    Register-ScheduledTask -TaskName "Body app weekly fetch" -Action $fetchAction -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $SundayAt) -Settings $settings -Description "Fetches this week's Body section from the hosted server for the Personal Sunday review" -Force | Out-Null

    $removedMsg = if ($removed.Count) { "Quitadas: $($removed -join ', ')." } else { "No habia tareas locales que quitar." }
    Write-Host "Registradas: Body app shift upload a las $At (desde $ShiftsDir), Body app weekly fetch los domingos a las $SundayAt. $removedMsg"
    exit 0
}

if (-not $At) { $At = "09:30" }

$dailyActions = @(New-ScheduledTaskAction -Execute $py -Argument "-m body.vivo pull --days 2" -WorkingDirectory $LiveDir)
if ($ShiftsDir) {
    $dailyActions += New-ScheduledTaskAction -Execute $py -Argument "-m body.vivo shift_import --dir `"$ShiftsDir`"" -WorkingDirectory $LiveDir
}
Register-ScheduledTask -TaskName "Body app daily sync" -Action $dailyActions -Trigger (New-ScheduledTaskTrigger -Daily -At $At) -Settings $settings -Description "Pulls Garmin data into the local Body app database" -Force | Out-Null

$pull = New-ScheduledTaskAction -Execute $py -Argument "-m body.vivo pull --days 2" -WorkingDirectory $LiveDir
$file = New-ScheduledTaskAction -Execute $py -Argument "-m body.vivo vida_export --current" -WorkingDirectory $LiveDir
Register-ScheduledTask -TaskName "Body app weekly file" -Action @($pull, $file) -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $SundayAt) -Settings $settings -Description "Writes Body's weekly vida-v1 file for the Personal Sunday review" -Force | Out-Null

$morningAction = New-ScheduledTaskAction -Execute $py -Argument "-m body.vivo manana" -WorkingDirectory $LiveDir
$morningTriggers = @($MorningAt | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
Register-ScheduledTask -TaskName "Body app morning file" -Action $morningAction -Trigger $morningTriggers -Settings $settings -Description "Pulls today's Garmin data, then writes today's vida-v1 daily file for Personal's morning brief" -Force | Out-Null

$wakeAction = New-ScheduledTaskAction -Execute $py -Argument "-m body.vivo despertar" -WorkingDirectory $LiveDir
$wakeTrigger = New-ScheduledTaskTrigger -Daily -At "04:00"
$wakeTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At "04:00" -RepetitionInterval (New-TimeSpan -Minutes 20) -RepetitionDuration (New-TimeSpan -Hours 9)).Repetition
Register-ScheduledTask -TaskName "Body app wake check" -Action $wakeAction -Trigger $wakeTrigger -Settings $settings -Description "Checks last night's Garmin sleep every 20 minutes from 04:00 to 13:00 and writes the wake-time file for Personal's morning brief as soon as it is known" -Force | Out-Null

$webArgs = "-m body.vivo web --port $Port"
if ($Lan) { $webArgs += " --lan" }
$webAction = New-ScheduledTaskAction -Execute $webPy -Argument $webArgs -WorkingDirectory $LiveDir
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
$morningMsg = $MorningAt -join ', '
Write-Host "Registered: daily sync at $At (shift import: $shiftMsg), weekly file on Sundays at $SundayAt, morning file at $morningMsg, wake check every 20 min from 04:00 to 13:00 (all run later if the PC was off). $webMsg"
