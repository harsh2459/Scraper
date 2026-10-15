# Configure this Windows PC only. Does not start scraping or connect a VPN.
param([string]$CoordinatorUrl = 'https://mytmtracker.com', [switch]$UseClipboard)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$coordinator = [Uri]$CoordinatorUrl
if ($coordinator.Scheme -ne 'https' -or $coordinator.UserInfo -or $coordinator.Query -or $coordinator.Fragment -or $coordinator.AbsolutePath -ne '/') {
    throw 'Use the HTTPS server origin, for example https://mytmtracker.com.'
}
$baseUrl = $CoordinatorUrl.TrimEnd('/')
$health = Invoke-RestMethod -Uri "$baseUrl/api/control/v1/health" -TimeoutSec 15
if ($health.status -ne 'ok') { throw 'Server fleet API is not ready. Configure its HTTPS route before registering this PC.' }
$vpnDirectory = Join-Path $workspace '.runtime\vpn-pilot\private'
if (-not (Test-Path -LiteralPath (Join-Path $vpnDirectory 'wg-pilot-ca13.conf'))) {
    throw 'Prepare the private VPN first: .\deploy\Invoke-WorkerVpnPilot.ps1 -PrepareOnly'
}
$settingsPath = Join-Path $workspace '.runtime\live-worker\worker.env'
if (Test-Path -LiteralPath $settingsPath) { throw 'Live worker config already exists. Keep it; do not overwrite its credentials or outbox.' }
$taskPython = Join-Path $workspace 'shared-scraper\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { $taskPython = (Get-Command python).Source }
$pointer = [IntPtr]::Zero
try {
    if ($UseClipboard) {
        $workerCredential = [string](Get-Clipboard -Raw)
    } else {
        $secureToken = Read-Host 'Paste the one-time token from Register worker (hidden)' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        $workerCredential = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    $workerCredential = $workerCredential.Trim()
    if ($workerCredential.Length -lt 24 -or $workerCredential -match '[\r\n\s]') { throw 'Invalid worker credential.' }
    $values = @{
        WORKER_COORDINATOR_URL=$baseUrl
        WORKER_TOKEN=$workerCredential
        WORKER_STORAGE_DIR=(Join-Path $workspace '.runtime\live-worker\outbox')
        WORKER_PORT='5011'; WORKER_BATCH_SIZE='1'; WORKER_BATCH_SECONDS='1200'
        WORKER_MODE='live'; WORKER_NETWORK_MODE='offline'; WORKER_NETWORK_CHECK_SECONDS='30'
        WORKER_PORTAL_HOSTS='tmrsearch.ipindia.gov.in'; WORKER_FULL_CHECK_DAYS='30'
        WORKER_MAX_PENDING='100'; WORKER_MIN_FREE_MB='256'
        WORKER_VPN_CONFIG='wg-pilot-ca13'; WORKER_VPN_CONFIG_DIR=$vpnDirectory
        WORKER_ACCOUNT_FILE=''; SCRAPER_LOAD_LEGACY_ENV='0'; SCRAPER_EXTERNAL_VPN_CONTROL='1'
    }
    $saveCode = @'
import json,sys,secrets
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from deploy.drive_account_repair import private_folder
values=json.load(sys.stdin)
values['WORKER_LOCAL_TOKEN']=secrets.token_urlsafe(36)
path=Path(sys.argv[1])
private_folder(path.parent)
for key,value in values.items():
    if not key.replace('_','').isalnum() or any(c in value for c in '\r\n\0'):
        raise SystemExit('Invalid configuration value; nothing saved.')
with path.open('x',encoding='utf-8') as handle:
    handle.write(''.join(key+'='+value+'\n' for key,value in sorted(values.items())))
'@
    $values | ConvertTo-Json -Compress | & $taskPython -c $saveCode $settingsPath $workspace
    if ($LASTEXITCODE -ne 0) { throw 'Could not securely save worker configuration.' }
} finally {
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    $workerCredential = $null
    $values = $null
}
Write-Host 'Live configuration saved privately: .runtime\live-worker\worker.env'
Write-Host 'Batch size: 1. Local outbox is separate from the demo. Nothing has started.'
Write-Host 'Assign a portal account to this machine in the server admin panel before starting it.'
