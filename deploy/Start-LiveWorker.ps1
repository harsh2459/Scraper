# Run the live worker and keep it running (restarts it 30 seconds after any exit or crash).
# Needs Administrator rights because it brings WireGuard tunnels up and down.
$ErrorActionPreference = 'Stop'
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Run this window as Administrator (right-click PowerShell, Run as administrator).' -ForegroundColor Red; exit 1
}
$root = Split-Path -Parent $PSScriptRoot
$rt = Join-Path $root '.runtime'
$log = Join-Path $rt 'worker-agent.err'
Get-Content (Join-Path $rt 'live-worker\worker.env') | ForEach-Object { if ($_ -match '^([A-Z][A-Z0-9_]*)=(.*)$') { Set-Item "env:$($Matches[1])" $Matches[2] } }
Set-Location (Join-Path $root 'shared-scraper')
$py = 'C:\Users\PC-7\AppData\Local\Python\pythoncore-3.14-64\python.exe'
if (-not (Test-Path $py)) { $py = (Get-Command python).Source }
while ($true) {
    Add-Content $log ("{0} [launcher] starting worker" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    & $py -m worker_agent *>> $log
    Add-Content $log ("{0} [launcher] worker exited with code {1}; restarting in 30 s" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $LASTEXITCODE)
    Start-Sleep -Seconds 30
}
