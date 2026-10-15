# Role launcher for the copied workspace. Prepare the demo before -Mode demo.
param(
  [ValidateSet('demo','coordinator','worker','legacy')][string]$Mode = 'demo',
  [string]$ConfigFile,
  [int]$FrontendPort = 5185,
  [switch]$Stop
)
$ErrorActionPreference = 'Stop'
$workspace = $PSScriptRoot
$sourceRoot = Join-Path $workspace 'shared-scraper'
$runtime = Join-Path $workspace '.runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$registry = Join-Path $runtime ("processes-$Mode.json")
if ($Stop) {
  if (Test-Path -LiteralPath $registry) {
    foreach ($record in (Get-Content -LiteralPath $registry -Raw | ConvertFrom-Json)) {
      $process = Get-Process -Id $record.Id -ErrorAction SilentlyContinue
      if ($process -and $process.StartTime.ToUniversalTime().ToString('o') -eq $record.StartTime) {
        Stop-Process -Id $record.Id
      }
    }
    Remove-Item -LiteralPath $registry
  }
  return
}
if (Test-Path -LiteralPath $registry) { throw 'Launch registry exists. Stop the recorded services before restarting.' }
if ($Mode -ne 'legacy') {
  if (-not $ConfigFile -and $Mode -eq 'demo') { $ConfigFile = Join-Path $runtime 'fleet-demo.env' }
  if (-not $ConfigFile -or -not (Test-Path -LiteralPath $ConfigFile)) { throw 'Provide an explicit configuration file; see deploy/FLEET-RUNBOOK.md.' }
  foreach ($line in Get-Content -LiteralPath $ConfigFile) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
      [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
  }
}
$taskPython = Join-Path $sourceRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { $taskPython = (Get-Command python).Source }
$records = @()
if ($Mode -in 'demo','coordinator') {
  # Browser operators sign in normally; only the user backend receives this token.
  $env:TM_CONTROL_ADMIN_TOKEN = $env:CONTROL_ADMIN_TOKEN
  $env:TM_CONTROL_API_BASE = 'http://127.0.0.1:5010'
}
$taskNode = (Get-Command node).Source
$viteScript = Join-Path $sourceRoot 'tm_manager\frontend\node_modules\vite\bin\vite.js'
function Start-TaskService($Name, $Executable, $Arguments, $Directory, $Port) {
  $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
  try { $probe.Start() } catch { throw "Port $Port is already in use." } finally { $probe.Stop() }
  $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $Directory -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $runtime "$Name.log") -RedirectStandardError (Join-Path $runtime "$Name.err")
  $script:records += @{Name=$Name; Id=$process.Id; StartTime=$process.StartTime.ToUniversalTime().ToString('o')}
  $script:records | ConvertTo-Json | Set-Content -LiteralPath $registry
}
if ($Mode -in 'demo','coordinator') {
  Start-TaskService 'control-plane' $taskPython @('-m','control_plane') $sourceRoot 5010
  Start-TaskService 'user-backend' $taskPython @('app.py') (Join-Path $sourceRoot 'tm_manager') 5005
  Start-TaskService 'user-frontend' $taskNode @($viteScript,'--host','127.0.0.1','--port',"$FrontendPort",'--strictPort') (Join-Path $sourceRoot 'tm_manager\frontend') $FrontendPort
}
if ($Mode -in 'demo','worker') {
  Start-TaskService 'worker-agent' $taskPython @('-m','worker_agent') $sourceRoot 5011
}
if ($Mode -eq 'legacy') {
  Start-TaskService 'legacy-scraper' $taskPython @('app.py') $sourceRoot 5001
  Start-TaskService 'legacy-user-backend' $taskPython @('app.py') (Join-Path $sourceRoot 'tm_manager') 5005
  Start-TaskService 'legacy-pdf' $taskPython @('app.py') (Join-Path $sourceRoot 'pdf_scraper') 5002
  Start-TaskService 'legacy-user-frontend' $taskNode @($viteScript,'--host','127.0.0.1','--port',"$FrontendPort",'--strictPort') (Join-Path $sourceRoot 'tm_manager\frontend') $FrontendPort
}
Start-Sleep -Seconds 2
foreach ($record in $records) {
  if (-not (Get-Process -Id $record.Id -ErrorAction SilentlyContinue)) { throw "$($record.Name) exited. Read .runtime/$($record.Name).err and stop the recorded services before retrying." }
}
Write-Host 'Services launched. Readiness and errors are logged in .runtime.'
if ($Mode -in 'demo','coordinator','legacy') { Write-Host "Portfolio: http://127.0.0.1:$FrontendPort/    Administration: http://127.0.0.1:$FrontendPort/admin/" }
if ($Mode -in 'demo','worker') { Write-Host 'Worker: http://127.0.0.1:5011/' }
