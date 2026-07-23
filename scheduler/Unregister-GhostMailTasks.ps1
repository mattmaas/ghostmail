# Removes all GhostMail scheduled tasks. Rollback for Register-GhostMailTasks.ps1.
$ErrorActionPreference = 'Continue'
Get-ScheduledTask -TaskPath '\GhostMail\' -ErrorAction SilentlyContinue | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_.TaskName -TaskPath '\GhostMail\' -Confirm:$false
    Write-Host "unregistered \GhostMail\$($_.TaskName)"
}
