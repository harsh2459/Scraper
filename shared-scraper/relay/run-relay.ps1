# Postgres relay - run on a machine with plain internet access (no VPN)
# that harsh-local-01 (or any VPN-restricted worker) can reach over the
# LAN. Forwards port 5432 straight through to Contabo.
#
# Usage:   powershell -ExecutionPolicy Bypass -File .\run-relay.ps1
# Stop:    powershell -ExecutionPolicy Bypass -File .\run-relay.ps1 -Stop

param([switch]$Stop)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

if ($Stop) {
  $c = Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue
  if ($c) {
    $c.OwningProcess | Select-Object -Unique | ForEach-Object {
      Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
      "stopped pid $_"
    }
  } else { "port 5432 already free" }
  return
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  Write-Error "Python not found on PATH. Install Python 3.10+ first."
  exit 1
}

if (Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue) {
  Write-Warning "Port 5432 is already in use on this machine - stop it first (run-relay.ps1 -Stop), or check whether Postgres is already installed here."
}

Write-Host "Starting Postgres relay -> Contabo (62.146.239.158:5432)..."
Start-Process -FilePath $python.Source -ArgumentList "pg_relay.py" -WorkingDirectory $root `
  -RedirectStandardOutput "$root\relay.log" -RedirectStandardError "$root\relay.err" `
  -WindowStyle Hidden -PassThru | Select-Object @{n='service';e={'pg-relay'}}, Id

Start-Sleep -Seconds 3
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch "Loopback|Tailscale|vEthernet" } | Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "Relay running. Point worker machines' PG_HOST at: $ip"
Write-Host "Check relay.log / relay.err in this folder if something looks wrong."
