# Registers GhostMail scheduled tasks under \GhostMail\. Idempotent (re-run to apply changes).
#   gm-sort   : incremental inbox sweep every 3h from 06:15 (labels, stars actions, records items)
#   gm-digest : daily 08:05 -> draft action digest to inbox + desktop notification
#
# Project root defaults to the parent of this script's folder (scheduler\..).
# Override with -ProjectRoot if the repo lives elsewhere.
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)
$ErrorActionPreference = 'Stop'
$runner = Join-Path $ProjectRoot 'scheduler\Run-GhostMail.ps1'
$psexe  = 'powershell.exe'

function Register-GM($name, $task, $trigger) {
    $action = New-ScheduledTaskAction -Execute $psexe `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Task $task" `
        -WorkingDirectory $ProjectRoot
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
    Unregister-ScheduledTask -TaskName $name -TaskPath '\GhostMail\' -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $name -TaskPath '\GhostMail\' -Action $action `
        -Trigger $trigger -Settings $settings -RunLevel Limited -Force | Out-Null
    Write-Host "registered \GhostMail\$name"
}

# gm-sort: daily 06:15 then repeat every 3h for 18h (06:15,09:15,...,21:15)
$tSort = New-ScheduledTaskTrigger -Daily -At 6:15AM
$tSort.Repetition = (New-ScheduledTaskTrigger -Once -At 6:15AM `
    -RepetitionInterval (New-TimeSpan -Hours 3) `
    -RepetitionDuration (New-TimeSpan -Hours 18)).Repetition
Register-GM 'gm-sort' 'sort' $tSort

# gm-digest: daily 08:05
$tDig = New-ScheduledTaskTrigger -Daily -At 8:05AM
Register-GM 'gm-digest' 'digest' $tDig

Write-Host "`nGhostMail tasks:"
Get-ScheduledTask -TaskPath '\GhostMail\' | Format-Table TaskName, State -AutoSize
