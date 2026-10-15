# Start/stop this fleet worker's services. Run setup.ps1 first (once, on a
# fresh machine) - this script just starts/stops what setup.ps1 already
# configured.
#
# Usage:   powershell -ExecutionPolicy Bypass -File .\run-worker.ps1
# Stop:    powershell -ExecutionPolicy Bypass -File .\run-worker.ps1 -Stop

param([switch]$Stop)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venvPython = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
  Write-Error "No .venv found - run setup.ps1 first."
  exit 1
}

$ports = 5001, 5005, 5002

if ($Stop) {
  foreach ($p in $ports) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($c) {
      $c.OwningProcess | Select-Object -Unique | ForEach-Object {
        Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        "stopped pid $_ on port $p"
      }
    } else { "port $p already free" }
  }
  return
}

$certifiOut = & $venvPython -c "import certifi; print(certifi.where())" 2>$null
if ($certifiOut) {
  $env:CURL_CA_BUNDLE     = $certifiOut
  $env:SSL_CERT_FILE      = $certifiOut
  $env:REQUESTS_CA_BUNDLE = $certifiOut
}

foreach ($p in 5001, 5005) {
  if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) {
    Write-Warning "port $p is already in use - run with -Stop first, or ignore if it's the right process."
  }
}

function Start-Svc($name, $file, $cmdArgs, $wd, $out, $err) {
  Start-Process -FilePath $file -ArgumentList $cmdArgs -WorkingDirectory $wd `
    -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru |
    Select-Object @{n='service';e={$name}}, Id
}

Start-Svc 'data-scraper' $venvPython 'app.py' $root `
  "$root\_run_scraper.log" "$root\_run_scraper.err"

Start-Svc 'tm-manager-api' $venvPython 'app.py' "$root\tm_manager" `
  "$root\tm_manager\_run_be.log" "$root\tm_manager\_run_be.err"

try {
  Start-Svc 'pdf-scraper' $venvPython 'app.py' "$root\pdf_scraper" `
    "$root\pdf_scraper\_run_pdf.log" "$root\pdf_scraper\_run_pdf.err"
} catch {
  Write-Host "pdf-scraper did not start (non-fatal)." -ForegroundColor Yellow
}

Start-Sleep -Seconds 10

Write-Host "`n--- health check ---"
foreach ($u in @('http://localhost:5001/', 'http://localhost:5005/api/dashboard')) {
  try {
    $r = Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 12
    "{0,-45} {1}" -f $u, $r.StatusCode
  } catch {
    if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) {
      "{0,-45} 401 (expected - needs login, process is up)" -f $u
    } else {
      "{0,-45} NOT READY (check logs in this folder)" -f $u
    }
  }
}
Write-Host "`nLocal status page: http://localhost:5001/status"
