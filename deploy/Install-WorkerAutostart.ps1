# One-time setup (run once as Administrator): starts the live worker automatically every time you sign in
# to Windows, with the rights it needs for the VPN, and restarts it if it stops.
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Run this window as Administrator (right-click PowerShell, Run as administrator).' -ForegroundColor Red; exit 1
}
$script = Join-Path $PSScriptRoot 'Start-LiveWorker.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $id.Name
$principal = New-ScheduledTaskPrincipal -UserId $id.Name -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'TM Tracker Live Worker' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'TM Tracker Live Worker'
Write-Host 'Done. The worker is starting now and will start by itself every time you sign in.'
Write-Host 'Log: .runtime\worker-agent.err   To stop it for good: Unregister-ScheduledTask -TaskName "TM Tracker Live Worker" -Confirm:$false'
