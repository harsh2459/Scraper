# Worker VPN routing and coordinator connectivity

This refines the approved fleet plan after the supplied VPN report. The configuration audit below was repeated in the copied workspace without printing key material. No tunnel was started, config changed, production database accessed or active-tunnel network test performed during this update.

## Evidence and limits

| Item | Independently checked in this copy |
|---|---|
| Configuration directory | `shared-scraper/vpn-configs`, 21 `.conf` files |
| Allowed destination | All 21 contain `AllowedIPs = 141.148.213.237/32` |
| IPv4 DNS | All 21 contain `10.2.0.1` |
| IPv6 DNS | Eight also contain `2a07:b944::2:1` |
| Worker behavior | Default offline cycle disconnects before delivery; optional split cycle verifies routes/HTTPS before delivering while connected |
| Connectivity with tunnel active | Not tested; configuration alone does not establish this |

The supplied report's installation versions, broader filesystem search, timestamps, usage totals and comparison with the other application copy were not independently re-established here. They are not deployment acceptance evidence.

WireGuard for Windows adds routes for the peers' allowed destinations. A `/32` route is scoped to an IP address, not an application: another browser/process accessing that address also uses the tunnel. The current configs therefore express destination-based split routing. Actual traffic still depends on the machine's other routes, DNS and firewall/VPN software. [WireGuard Windows network behavior](https://git.zx2c4.com/wireguard-windows/about/docs/netquirk.md).

The DNS addresses are outside the configured allowed range. They may be unreachable through the intended tunnel, although other machine routes can affect that outcome. Do not infer successful DNS from a running tunnel service. Resolve and test while connected. The portal's current A and AAAA records, redirects and document hosts also require verification; a stale single IPv4 route can send a changed destination outside the VPN.

Each worker needs independently provisioned provider credentials/configs. Copying one key to simultaneous machines can make the server peer's learned endpoint move between them. Provisioning separate keys also does not establish account concurrency entitlement: verify the provider plan before adding workers. Keep private keys on their worker, outside UI responses and logs. [WireGuard peer endpoint behavior](https://www.wireguard.com/#cryptokey-routing), [Proton configuration generation](https://protonvpn.com/support/wireguard-configurations).

## Architecture decision

Retain WireGuard and the existing SQLite assignments/outbox. No proxy is required merely because Contabo and the portal have different destination routes. If strict isolation to one process is required, a separate network environment or supported application routing needs its own design and pilot.

The default mode uses a bounded batch: reconcile/upload, reserve and save work, connect, scrape/checkpoint, disconnect, upload. Optional online transport is now implemented but not enabled in the demo or verified with a live tunnel. The existence of a `/32` config does not activate online behavior. Live workers now validate reviewed portal destinations and actual tunnel routes before portal requests in either mode.

The implemented split mode permits coordinator heartbeats and delivery through the normal connection while portal requests use WireGuard. Every bundle is persisted before delivery, with all reservation/generation checks retained. Internet loss or a coordinator outage can happen regardless of routing. Coordinator failure preserves work and uses the bounded recovery cycle. A portal route failure blocks portal requests until routing is repaired. The HTTPS adapter pins new connections to vetted destinations, validates the original hostname/certificate, guards redirects and rechecks reused connections; environment proxies are disabled for this worker path.

The queue remains PostgreSQL on Contabo. Workers never need PostgreSQL credentials or access to port 5432; both routing modes use the coordinator HTTPS API. BullMQ remains a separate optional server-side decision.

## One-machine pilot

Run on the intended Windows worker using its own provider config and the installed WireGuard client. First drain/stop the scraper so a manual tunnel test does not race its lifecycle controller. Starting/stopping a tunnel requires the appropriate local privileges. These checks must not print configs, private keys or `wg show ... dump` output.

1. Before connecting, resolve `tmrsearch.ipindia.gov.in` to all A/AAAA addresses. Compare those addresses with the config's allowed destinations. Record the approved portal/redirect/document destinations rather than adding broad default routes. Establish a baseline for ordinary DNS and coordinator HTTPS.
2. Choose the DNS policy before deployment. Either keep ordinary machine DNS by removing the tunnel DNS override from a separately prepared worker config, or deliberately route the required provider DNS hosts through the VPN and verify both address families. Normal DNS can reveal queried hostnames to the ordinary resolver; provider DNS changes can affect other applications. Do not add `0.0.0.0/0` or `::/0` merely to fix DNS. No DNS/config edits have been made by this plan update.
3. Start one named test tunnel. Verify its service, usable portal route and successful portal HTTPS connection. A running service is insufficient. Check the current portal IPs again while connected and verify document/redirect destinations; an uncovered usable AAAA record must not bypass the tunnel.
4. Verify the coordinator hostname resolves and its selected route stays outside this WireGuard interface. Request the actual coordinator's HTTPS health endpoint with normal TLS validation. Check ordinary lookups/browsing too; a successful raw-IP connection would miss hostname/DNS failures.
5. Stop the test tunnel in a `finally` block, confirm its service is stopped/removed and repeat the baseline checks. Restore any temporary worker config separately; do not overwrite the original exported keys/config set.
6. Only after routing succeeds, perform the staging worker's authenticated heartbeat, fixture artifact upload and acceptance cycle. A health response proves neither authorization nor durable acceptance. Record DNS results, route interfaces, HTTP outcomes and whether cleanup succeeded, with secrets excluded.

For the read-only coordinator probe, replace the example with the real staging hostname; the control API must already be deployed behind HTTPS:

```powershell
$coordinatorBase = [uri]'https://your-contabo-host.example'
$healthUri = [uri]::new($coordinatorBase, '/api/control/v1/health')
Invoke-RestMethod -Uri $healthUri -TimeoutSec 15
```

The health path is implemented by `control_plane/app.py` and forwarded by `nginx-fleet.locations.conf`. It needs no database password or bearer token. Do not disable certificate validation or substitute a production database connection test.

## Configuration and read-only audit

The worker settings are implemented in `worker_agent/config.py`; examples are in `worker.env.example`:

- `WORKER_NETWORK_MODE=offline` is the default. Set `split` explicitly only on a prepared worker; the existing live-mode/tunnel/individual token settings are still required.
- `WORKER_NETWORK_CHECK_SECONDS=30` controls periodic checks/heartbeats during scraping (10–300 seconds). Checks occur before portal requests and online delivery too. Heartbeats do not renew the reservation or grant new work.
- `WORKER_PORTAL_HOSTS=tmrsearch.ipindia.gov.in` is the default reviewed hostname list. Additional redirect/document hosts require explicit review and config coverage. Both A and AAAA addresses must be covered. Unreviewed redirects, plain HTTP, proxies and disabled TLS validation are rejected.
- In split mode, configured DNS IPs must also be covered by AllowedIPs. Actual DNS routes must use the owned tunnel. A config without the DNS directive keeps ordinary machine DNS. Prepare this policy explicitly; the 21 copied exports have not been edited and will fail split preflight until their DNS policy is repaired. Full-tunnel configs remain supported in offline mode.
- Requested/effective mode, last portal/coordinator checks, timestamps and fallback reasons appear on the local worker UI and in worker details on the administration UI. Fixture checks are labelled simulated and do not establish live VPN connectivity.

From `shared-scraper`, audit a prepared worker config without starting a tunnel:

```powershell
python -m worker_agent.network_check --vpn-config YOUR_WORKER_CONFIG --coordinator-url https://your-contabo-host.example
```

This reads only the public routing fields and resolves the reviewed portal hosts. It performs no HTTPS probes or tunnel lifecycle changes. With an already running test tunnel, add `--connected` to check actual routes, portal HTTPS and coordinator HTTPS health. It prints key-free JSON, uses normal certificate validation, needs no worker/DB credentials, performs no portal login/OTP, and never prints response bodies. A connected audit is a diagnostic; still complete the authenticated staging acceptance tests in the pilot above.

## Implemented recovery and verification

1. Before claiming new portal work, preflight checks the reviewed config/DNS and portal address coverage. A failed preflight leaves that work queued without consuming a server attempt. Saved-PDF repair uses a restricted repair-only claim and requires no portal preflight or VPN connection. After connecting and before each guarded portal request, native Windows destination routes must select the owned tunnel. Changed configuration or uncovered destinations stop requests; active configs are never rewritten in place.
2. A periodic monitor verifies portal routing and coordinator DNS/routes/HTTPS, then reports a heartbeat. Saved items can upload while these checks pass. Drain/pause prevents subsequent item starts; a portal failure stops remaining scrapes. The monitor stops before tunnel teardown, so a stale monitor cannot race another cycle.
3. Coordinator loss continues only saved assignments within the existing reservation/batch budget. Upload failures and lost acknowledgments retain the same bundle for idempotent retry. Cleanup disconnects the owned tunnel and verifies its state before normal delivery/reconciliation. Failed cleanup postpones coordinator contact. No additional assignments are claimed during the connected batch.
4. Isolated tests cover live-route logic with synthetic configs, DNS/IPv6/config changes, proxies/redirects/TLS rejection, real loopback certificate validation with pinned IPs, online/offline cycles, coordinator outages, interrupted delivery, lost acknowledgments, restart reuse, route failures and draining. The original offline tests still prohibit coordinator calls while the simulated tunnel is active.

Live tunnel/portal connectivity, provider provisioning/connection entitlement, Contabo deployment and two independently provisioned staging workers remain pilot checks. No live VPN, production database or copied private-key config was changed during implementation.

## Route performance and claim ordering correction

The reported issues were reproduced in the code: route guards previously started PowerShell at every adapter/connection/delivery check, and local preflight ran after reservation and offline acknowledgment. Both paths have been corrected.

`worker_agent/network.py` now uses the in-process Windows IP Helper API for both IPv4 and IPv6. Native failures stop verification; no subprocess or guessed interface fallback is used. See Microsoft's [GetBestInterfaceEx](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getbestinterfaceex), [ConvertInterfaceIndexToLuid](https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-convertinterfaceindextoluid) and [ConvertInterfaceLuidToAlias](https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-convertinterfaceluidtoalias) contracts.

Successful route lookups are cached for at most **two seconds**, shared by the repeated guard layers. Config metadata and DNS resolution are checked on every guard call; DNS is not cached by the worker. A changed destination set, config change, verification failure, preflight or owned-tunnel lifecycle change discards cached routes. TLS hostname validation, pinned destinations, redirect/proxy restrictions and keep-alive destination checks remain enabled. An unrelated operating-system route change can take up to two seconds to be detected; it is not an immediate route-change notification mechanism.

A read-only measurement on this Windows machine checked IPv4 loopback, IPv6 loopback and the portal destination together. Across 100 calls, the new native lookup had a **0.116 ms median** (first call 0.770 ms); three equivalent fresh PowerShell calls had a **1,911.494 ms median**. All selected interfaces matched. These timings measure route selection, not DNS, HTTP or scraping. No tunnel or network connection was started. The key-free evidence is `.runtime/vpn-route-benchmark.json`.

The coordinator's optional Boolean `repair_only` claim filter never reserves portal jobs and preserves outstanding reservation authority. The worker first checks for saved-PDF repair, then runs local preflight before the ordinary scraping claim. Six consecutive failed preflights were tested against the isolated PostgreSQL database: the portal job remained queued with zero attempts, then completed on its first attempt after recovery. Repairs still work alongside queued portal jobs with broken VPN preflight. Failures occurring after a successful preflight, such as tunnel startup or portal failure, still follow the existing reservation/retry policy; this change does not refund their attempts.

Deploy the updated coordinator **before** the updated workers so the repair-only claim filter is available. No database migration is required for this correction. Verification: **96 automated tests pass**, including real Windows IPv4/IPv6 route queries without subprocesses, route-cache expiry/invalidation, existing loopback TLS checks and the PostgreSQL attempt-count assertions. Live tunnel and Contabo acceptance remain unverified.

The transport release passed 87 isolated tests, both management builds, TypeScript and the hidden-browser review. Review screenshots are `.runtime/network-admin.png` and `.runtime/network-worker.png`. The demo remains in fixture/offline mode.

## Live pilot execution status

Preparation has now been executed on this machine, with real portal DNS resolution and the key-free configuration audit. WireGuard 1.1.1 is installed; no tunnel was running. The shell has no Windows administrator token, including outside the filesystem sandbox. A connected pilot also needs the actual staging coordinator HTTPS URL; the only configured coordinator found is the localhost fixture demo.

- Prepared `.runtime/vpn-pilot/private/wg-pilot-ca13.conf` from the existing `wg-CA-FREE-13` export. This uses the same key on this one machine; it does not provision another worker/provider identity.
- Removed the DNS override only from that separate private copy and set exact routes from current portal DNS. The current address is `141.148.213.237`, so the allowed route remains `/32`.
- Verified the private folder's protected ACL grants access only to the current user, SYSTEM and Administrators. The private key is preserved and has not been printed. All 21 original exports remain unchanged.
- Real preflight passed: `portal=configuration_checked`, `simulated=false`. No connected route, handshake, portal HTTPS or Contabo acceptance result is claimed.
- Key-free execution evidence is in `.runtime/vpn-pilot/pilot-report.json`. Its current phase is `prepared`; coordinator URL and Windows elevation remain prerequisites.

The reproducible helper is [Invoke-WorkerVpnPilot.ps1](Invoke-WorkerVpnPilot.ps1). To repeat preparation without installing a tunnel:

```powershell
.\deploy\Invoke-WorkerVpnPilot.ps1 -PrepareOnly
```

After stopping the live scraper on this machine and supplying the actual deployed staging hostname, run the connected pilot from an **administrator PowerShell** at the workspace root:

```powershell
.\deploy\Invoke-WorkerVpnPilot.ps1 -CoordinatorUrl 'https://YOUR-STAGING-HOST'
```

The helper checks baseline coordinator HTTPS, installs only the uniquely named pilot tunnel, runs the guarded portal/coordinator audit, removes that tunnel in `finally`, verifies removal and checks coordinator HTTPS again. It refuses other active tunnels or an already installed pilot service. It discards native command diagnostics that might contain keys. It never starts the scraping worker or accesses PostgreSQL. Authenticated staging artifact acceptance and the second worker remain subsequent pilot steps. Service commands follow the [official WireGuard Windows CLI](https://git.zx2c4.com/wireguard-windows/about/docs/enterprise.md).

Prepared configs outside the export folder are supported by `WORKER_VPN_CONFIG_DIR` and the audit's `--config-dir`. Set `WORKER_VPN_CONFIG=wg-pilot-ca13` and the private directory only after successful routing and authenticated staging checks; do not enable live workers from this preparation alone.

Verification after adding private-directory support: **89 isolated tests passed**, including guard/audit directory selection and the WireGuard client path. The PowerShell helper parsed successfully and its preparation branch was executed; connected service installation remains untested because Windows elevation and the actual coordinator URL are unavailable.
