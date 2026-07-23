param(
    [ValidateSet('sort','digest')][string]$Task='sort',
    # Project root = parent of this script's folder (scheduler\..). Override if needed.
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    # Python interpreter: GHOSTMAIL_PYTHON env var wins, otherwise 'python' from PATH.
    [string]$PythonExe = $(if ($env:GHOSTMAIL_PYTHON) { $env:GHOSTMAIL_PYTHON } else { 'python' })
)
# GhostMail scheduled runner. Logs to <ProjectRoot>\data\sched-logs\gm-<task>-<date>.log
$ErrorActionPreference = 'Continue'
Set-Location $ProjectRoot
$logdir = Join-Path $ProjectRoot 'data\sched-logs'
New-Item -ItemType Directory -Force -Path $logdir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd'
$log = Join-Path $logdir "gm-$Task-$stamp.log"
"=== $(Get-Date -Format o) gm-$Task START ===" | Out-File -Append -Encoding utf8 $log
if ($Task -eq 'sort') {
    # --no-archive = label/star only, nothing leaves the inbox.
    # Remove --no-archive to resume auto-archiving of noise buckets (the scheduled
    # task calls this script by path, so no re-registration is needed).
    & $PythonExe -m ghostmail.batch_sorter --mode incremental --days 45 --limit 400 --no-archive 2>&1 |
        Out-File -Append -Encoding utf8 $log
} else {
    & $PythonExe -m ghostmail.action_triage 2>&1 | Out-File -Append -Encoding utf8 $log
}
"=== $(Get-Date -Format o) gm-$Task EXIT $LASTEXITCODE ===" | Out-File -Append -Encoding utf8 $log
