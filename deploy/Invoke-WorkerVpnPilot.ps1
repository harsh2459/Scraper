# Prepare a private config, then optionally run a temporary, bounded VPN test.
# Neither branch starts a scraping worker or changes the original exports.
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9_-]{1,64}$')][string]$SourceConfig = 'wg-CA-FREE-13',
    [ValidatePattern('^wg-pilot-[A-Za-z0-9_-]{1,48}$')][string]$TunnelName = 'wg-pilot-ca13',
    [string]$CoordinatorUrl,
    [switch]$PrepareOnly
)
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$sourceRoot = Join-Path $workspace 'shared-scraper'
$pilotRoot = Join-Path $workspace '.runtime\vpn-pilot'
$privateRoot = Join-Path $pilotRoot 'private'
$reportPath = Join-Path $pilotRoot 'pilot-report.json'
$configPath = Join-Path $privateRoot ($TunnelName + '.conf')
$wireguardExe = 'C:\Program Files\WireGuard\wireguard.exe'
$workerPython = Join-Path $sourceRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $workerPython)) { $workerPython = (Get-Command python).Source }
$serviceName = 'WireGuardTunnel$' + $TunnelName
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$report = [ordered]@{
    checkedAt = [DateTime]::UtcNow.ToString('o')
    phase = 'preparing'
    dnsPolicy = 'ordinary-machine-dns'
    sourceConfig = $SourceConfig
    tunnelName = $TunnelName
    privateConfig = $configPath
    originalsModified = $false
    tunnelStarted = $false
    cleanupVerified = $null
    portalAddresses = @()
    coordinator = 'pending-url'
    authenticatedAcceptance = 'not-run'
    windowsAdministrator = $isAdministrator
    wireGuardInstalled = (Test-Path -LiteralPath $wireguardExe)
    activeTunnelNames = @()
    routingPilotBlockers = @(
        if (-not $CoordinatorUrl) { 'actual-coordinator-https-url' }
        if (-not $isAdministrator) { 'windows-administrator-session' }
    )
}

function Save-Report {
    [IO.File]::WriteAllText($reportPath, ($report | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
}

function Invoke-WireGuard([string]$Operation, [string]$Value) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $wireguardExe
    $info.Arguments = $Operation + ' "' + $Value + '"'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        # Discard diagnostics: a provider/config error could contain key material.
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            $process.Kill()
            throw 'WireGuard command timed out.'
        }
        if ($process.ExitCode -ne 0) { throw 'WireGuard command failed; inspect service privileges/configuration without printing keys.' }
    } finally { $process.Dispose() }
}

function Test-Coordinator {
    $reply = Invoke-RestMethod -Uri ($CoordinatorUrl.TrimEnd('/') + '/api/control/v1/health') -TimeoutSec 15 -MaximumRedirection 0
    if ($reply.status -ne 'ok') { throw 'Coordinator health endpoint did not return the expected status.' }
}

function Invoke-Audit([switch]$Connected) {
    # Preflight doesn't contact Contabo. This reserved hostname is used only for
    # validating argument shape when no actual endpoint was supplied.
    $auditUrl = if ($CoordinatorUrl) { $CoordinatorUrl } else { 'https://pending-coordinator.invalid' }
    $auditArgs = @('-m', 'worker_agent.network_check', '--vpn-config', $TunnelName,
                   '--config-dir', $privateRoot, '--coordinator-url', $auditUrl)
    if ($Connected) { $auditArgs += '--connected' }
    Push-Location $sourceRoot
    try {
        $output = @(& $workerPython @auditArgs 2>$null)
        $auditExit = $LASTEXITCODE
        $audit = (($output -join "`n") | ConvertFrom-Json)
        # The audit intentionally emits only key-free JSON. Retain failures
        # before cleanup so the useful diagnostic is not discarded.
        if ($Connected) { $report.connectedAudit = $audit } else { $report.configAudit = $audit }
        Save-Report
        if ($auditExit -ne 0) {
            Write-Output ($audit | ConvertTo-Json -Depth 6) | Out-Host
            throw 'Worker routing/HTTPS audit failed; see the key-free network check for details.'
        }
        return $audit
    } finally { Pop-Location }
}

# Private material is created only after the parent directory is protected.
foreach ($directory in @((Join-Path $workspace '.runtime'), $pilotRoot, $privateRoot)) {
    if (Test-Path -LiteralPath $directory) {
        if ((Get-Item -LiteralPath $directory).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Pilot directories must be real directories within this workspace.'
        }
    } else { [void](New-Item -ItemType Directory -Path $directory) }
}
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$security = [Security.AccessControl.DirectorySecurity]::new()
$security.SetOwner($currentSid)
$security.SetAccessRuleProtection($true, $false)
foreach ($sid in @($currentSid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
                   [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $security.AddAccessRule($rule)
}
$existingAcl = Get-Acl -LiteralPath $privateRoot
$allowedSids = @($currentSid.Value, 'S-1-5-18', 'S-1-5-32-544')
$existingRules = @($existingAcl.Access)
$unsafeRules = @($existingRules | Where-Object {
    $_.AccessControlType -ne 'Allow' -or
    $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $allowedSids -or
    $_.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl
})
# Avoid rewriting an already correct descriptor: repeated Set-Acl can request
# SACL privileges unnecessarily on Windows. Fail before creating keys if a
# needed protection change cannot be applied.
if (-not $existingAcl.AreAccessRulesProtected -or $existingRules.Count -ne 3 -or $unsafeRules.Count) {
    Set-Acl -LiteralPath $privateRoot -AclObject $security
}

try {
    $step = 'checking-existing-pilot-service'
    # Uninstall requests can return before Windows deletes a stopped service.
    # Wait briefly for that deletion; never remove a running worker's tunnel.
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    $existingPilot = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    while ($existingPilot -and $existingPilot.Status -ne 'Running' -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $existingPilot = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    }
    if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
        $report.routingPilotBlockers += 'existing-pilot-service'
        throw 'The named pilot service already exists; stop/remove that pilot before preparing a new config.'
    }
    if ($CoordinatorUrl) {
        $step = 'validating-coordinator-url'
        $coordinator = [uri]$CoordinatorUrl
        if (-not $coordinator.IsAbsoluteUri -or $coordinator.Scheme -ne 'https' -or $coordinator.UserInfo -or $coordinator.Query -or $coordinator.Fragment) {
            throw 'Provide a coordinator HTTPS base URL without credentials, query parameters or fragments.'
        }
        $report.coordinator = 'not-tested'
    }
    $original = Join-Path (Join-Path $sourceRoot 'vpn-configs') ($SourceConfig + '.conf')
    $step = 'reading-source-vpn-config'
    $raw = [IO.File]::ReadAllText($original)
    $step = 'validating-source-vpn-config'
    if ([regex]::Matches($raw, '(?im)^\s*\[Peer\]\s*$').Count -ne 1 -or
        [regex]::Matches($raw, '(?im)^\s*\[Interface\]\s*$').Count -ne 1 -or
        [regex]::Matches($raw, '(?im)^\s*AllowedIPs\s*=').Count -ne 1 -or
        $raw -match '(?im)^\s*(PreUp|PostUp|PreDown|PostDown)\s*=') {
        throw 'Pilot needs one ordinary interface/peer config without executable lifecycle directives.'
    }
    $step = 'resolving-portal-dns'
    $addresses = @([Net.Dns]::GetHostAddresses('tmrsearch.ipindia.gov.in') | ForEach-Object { $_.ToString() } | Sort-Object -Unique)
    if (-not $addresses.Count -or $addresses.Count -gt 32) { throw 'Portal DNS did not provide a bounded address set.' }
    $report.portalAddresses = $addresses
    $allowed = ($addresses | ForEach-Object { if ($_ -match ':') { $_ + '/128' } else { $_ + '/32' } }) -join ', '
    $prepared = [regex]::Replace($raw, '(?im)^[ \t]*DNS[ \t]*=.*(?:\r?\n|$)', '')
    $prepared = [regex]::Replace($prepared, '(?im)^[ \t]*AllowedIPs[ \t]*=.*$', ('AllowedIPs = ' + $allowed))
    $step = 'writing-private-vpn-config'
    [IO.File]::WriteAllText($configPath, $prepared, [Text.UTF8Encoding]::new($false))
    $step = 'checking-config-routes'
    $report.configAudit = Invoke-Audit
    $report.phase = 'prepared'
    Save-Report
    if ($PrepareOnly) {
        Write-Output ('Private pilot config prepared; key-free report: ' + $reportPath)
        return
    }
    if (-not $CoordinatorUrl) {
        $report.phase = 'awaiting-coordinator-url'
        throw 'Prepared config is ready; supply the actual coordinator HTTPS URL for the connected pilot.'
    }
    if (-not $isAdministrator) {
        $report.phase = 'awaiting-windows-elevation'
        throw 'Prepared config is ready; the connected pilot requires an elevated Windows PowerShell.'
    }
    if (-not (Test-Path -LiteralPath $wireguardExe)) { throw 'WireGuard is not installed at the standard path.' }
    $otherTunnels = @(Get-Service -ErrorAction Stop | Where-Object { $_.Name.StartsWith('WireGuardTunnel$') -and $_.Status -ne 'Stopped' })
    if ($otherTunnels.Count) {
        $report.activeTunnelNames = @($otherTunnels | ForEach-Object {
            $name = $_.Name.Substring('WireGuardTunnel$'.Length)
            if ($name -match '^[A-Za-z0-9_-]{1,64}$') { $name } else { 'unrecognized-name' }
        })
        $report.routingPilotBlockers += 'another-active-wireguard-tunnel'
        throw 'Another tunnel is active; drain/stop its scraper before the pilot.'
    }
    $step = 'checking-server-before-vpn'
    Test-Coordinator
    $report.coordinator = 'baseline-health-ok'
    $attempted = $false
    try {
        $attempted = $true
        $step = 'starting-pilot-tunnel'
        Invoke-WireGuard '/installtunnelservice' $configPath
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        do {
            $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
            if ($service -and $service.Status -eq 'Running') { break }
            Start-Sleep -Milliseconds 500
        } while ([DateTime]::UtcNow -lt $deadline)
        if (-not $service -or $service.Status -ne 'Running') { throw 'Pilot tunnel did not reach running state.' }
        $report.tunnelStarted = $true
        $step = 'checking-connected-routes-and-https'
        $report.connectedAudit = Invoke-Audit -Connected
        $report.coordinator = 'connected-health-ok'
        $report.phase = 'routing-pilot-passed'
    } finally {
        if ($attempted) {
            $failedStep = $step
            $step = 'removing-pilot-tunnel'
            if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
                Invoke-WireGuard '/uninstalltunnelservice' $TunnelName
            }
            $deadline = [DateTime]::UtcNow.AddSeconds(20)
            while ((Get-Service -Name $serviceName -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 500 }
            $report.cleanupVerified = -not [bool](Get-Service -Name $serviceName -ErrorAction SilentlyContinue)
            Save-Report
            if (-not $report.cleanupVerified) { throw 'Pilot cleanup failed; remove only the named pilot tunnel before resuming the scraper.' }
            $step = $failedStep
        }
    }
    $step = 'checking-server-after-vpn'
    Test-Coordinator
    $report.coordinator = 'post-cleanup-health-ok'
    Save-Report
    Write-Output ('Routing pilot passed and tunnel cleanup verified. Authenticated staging acceptance remains separate. Report: ' + $reportPath)
} catch {
    if ($report.phase -notlike 'awaiting-*') { $report.phase = 'incomplete' }
    # Exceptions from native commands, DNS and HTTP can include sensitive data.
    $report.failureStage = $step
    $report.exceptionType = $_.Exception.GetType().Name
    $report.error = 'Pilot failed at ' + $step + ' (' + $report.exceptionType + '). No keys were printed.'
    if ($report.routingPilotBlockers -contains 'another-active-wireguard-tunnel') {
        $report.error = 'Another WireGuard tunnel is running: ' + ($report.activeTunnelNames -join ', ') + '. Finish/stop its scraper, then stop that tunnel before rerunning the pilot.'
    }
    if ($report.routingPilotBlockers -contains 'existing-pilot-service') {
        $report.error = 'The pilot service still exists. Stop the worker, remove only ' + $TunnelName + ', wait for Windows service deletion, then rerun.'
    }
    Save-Report
    throw $report.error
}
