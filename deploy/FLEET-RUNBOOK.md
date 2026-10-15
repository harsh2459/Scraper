# Fleet implementation and local review

The runnable release and the six reviewed correctness gaps are implemented in this copy. The control API and user API share PostgreSQL; workers use HTTPS plus a SQLite outbox and do not receive database credentials. PostgreSQL implements the durable queue. BullMQ is not required for this release.

## Review the prepared demo

The isolated fixture demo uses `fleet_dev_control` on `127.0.0.1:55439`, separate from the integration-test database. It generates synthetic records and does not connect a VPN, read email, call IP India, or upload to Drive.

| Screen | Address | Sign-in |
|---|---|---|
| Portfolio | http://127.0.0.1:5185/ | `DEMO_ADMIN_EMAIL` and `DEMO_ADMIN_PASSWORD` |
| Fleet administration | http://127.0.0.1:5185/admin/ | `CONTROL_ADMIN_TOKEN`; Users/Plans/Subscriptions use the normal admin login |
| Local worker | http://127.0.0.1:5011/ | `WORKER_LOCAL_TOKEN` |

The credentials are in `.runtime/fleet-demo.env`. Keep this generated file private; it is ignored by Git. The demo already contains application `9000001` with an accepted synthetic observation and a downloadable fixture document. Change or add application numbers to exercise the full lifecycle.

From the workspace root:

```powershell
.\run-all.ps1 -Mode demo
# When finished, stop only services recorded by this launcher:
.\run-all.ps1 -Mode demo -Stop
```

If the demo is already running, open its URLs rather than launch it twice. The launcher checks ports, records process IDs/start times, uses hidden windows, and refuses duplicate launch registries. Logs are in `.runtime`. `-FrontendPort` changes the default 5185. Worker mode starts only the local agent, not the user backend or a PDF server.

The local PostgreSQL cluster prepared during implementation is `.runtime/postgres-test`. To restart it if needed:

```powershell
& 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe' -D "$PWD\.runtime\postgres-test" -l "$PWD\.runtime\postgres-test.log" -o '-h 127.0.0.1 -p 55439' start
```

This disposable cluster uses local trust authentication; it must remain loopback-only. Use password-authenticated service roles on staging/production. Do not point these demo/test scripts at Contabo.

## Rebuild and verify

Install the existing scraper/TM Manager dependencies plus `shared-scraper/control_plane/requirements.txt` in the chosen Python environment. The live adapter reuses existing scraper libraries and PDF extraction; a fixture worker needs only the new service dependencies. Use the existing frontend package lock when installing Node dependencies.

```powershell
Set-Location shared-scraper/tm_manager/frontend
npm ci
npm run build
npm run build:admin
npm run build:worker
npx tsc -p tsconfig.ops.json
npx tsc -b
Set-Location ../..
python -m unittest discover -s tests -v
```

Tests refuse non-loopback/non-`fleet_test` databases and default to `fleet_test_control` on port 55439. They cover concurrent claims, retries, expiry/late results, duplicate completion, upload integrity, outages/restarts, partial checkpoint recovery, shared portfolios, user permissions, encrypted account isolation, schedule rules/overrides, pause/drain, local API origin checks, and separate service grants. Test data is truncated; never use a database containing valuable records.

`scripts/setup_fleet_demo.py` creates the demo in a new local `fleet_dev_control` database when the generated config does not already exist. `migrations/prepare_legacy.py` creates legacy fixture tables from existing SQL literals without importing app entrypoints. It is deliberately restricted to disposable local databases; do not use it as a production migration mechanism.

`scripts/verify_fleet_demo.py` checks the running demo over HTTP and uses optional Python Playwright against a separately started loopback Chrome debugging instance at port 9224. Review screenshots are saved in `.runtime`; the browser check does not print credentials.

`python scripts/verify_correctness_ui.py` also verifies the new ledger/proceeding/event screens, receipt calculation, private deadline confirmation and the worker results screen in a hidden Chrome browser. It is restricted to the localhost fixture demo, seeds one synthetic notice on application `9000001`, and saves `correctness-*.png` screenshots in `.runtime`. It does not use the live portal/VPN.

The same review now checks fleet/worker network status and saves `network-admin.png` and `network-worker.png`. Current backend verification: 96 isolated tests pass, including native Windows routes/cache invalidation and preflight attempt protection. Both management builds, TypeScript and this browser review passed during the preceding UI release. See `VPN-ROUTING-PLAN.md` for the read-only audit command and live split-mode prerequisites.

## Configuration and operating contract

- `deploy/control.env.example`: explicit coordinator/demo values. For production use `CONTROL_ENV=production`, the control service database role, a random admin bearer token, a Fernet `CONTROL_ACCOUNT_KEY`, and the shared artifact directory. Back up the encryption key with restricted access; losing it makes stored account passwords unreadable.
- `deploy/user-fleet.env.example`: production user service configuration and a different database role. New mode defaults to a loopback bind and disables automatic schema changes. Set `TM_LOAD_LEGACY_ENV=0` and `TM_FLEET_CONTROL_ENABLED=1` explicitly.
- `deploy/worker.env.example`: per-machine token, local control token, storage directory, batch size, batch duration, and fixture/live mode. Each worker has a unique server-issued token. Remote coordinator URLs must use HTTPS. The local API binds to loopback and verifies its bearer token and browser origin.
- `CONTROL_PUBLISH_LEGACY=1` publishes accepted status/documents/history into the existing user read models in the same transaction as job completion. The user backend needs read access to the same artifact directory. Owners, notes and subscriptions are not changed by publication.
- A paused refresh policy has no automatic due date. An explicit schedule disables automatic status-rule assignment for that application. Rules match exact portal status, case-insensitively; when multiple rules match, the shortest non-paused interval wins. No legal deadline is inferred. Configure ten-year intervals only when you have decided they are appropriate for those applications.
- Workers upload pending local results before obtaining new work. Reservation identity and generation protect against stale results. Repeated delivery is safe; expired/cancelled results are archived as obsolete. Only a complete, accepted result advances the successful observation and next-fetch date.
- Pausing/draining a worker prevents new scraping but allows delivery. Drain before changing an account assignment. Retiring a worker revokes all API access, including delivery; drain first to preserve its pending results. Operator cancellation blocks automatic requeue for that generation; use Retry now to create/resume work.
- Local saved results are retained, including after acknowledgment. No automatic artifact deletion runs in this release. Monitor disk/backlog thresholds and retain unacknowledged files. The worker checks free space and backs off on failures.

## Staging, migration and cutover

1. Clone the existing database and artifact storage to staging. Inventory integrations and preserve current service/environment files. Rehearse `pg_dump --format=custom` plus `pg_restore` into a new staging database, and verify document files and ownership after restoration. Database-only backups do not contain uploaded artifact bytes or encryption keys.
2. Install dependencies and build all three frontends. Place the contents of `shared-scraper` at the existing `/opt/tmtracker` service root, or adjust the new units consistently. Build output must accompany both control and worker installations; the local worker serves its UI itself.
3. As the migration owner, explicitly set the reviewed `CONTROL_DATABASE_URL` and run `python -m migrations`. Migrations are additive, transactional and checksum tracked. Existing portfolio application numbers are backfilled into canonical schedules without changing private portfolio rows. Review the default four-week backfill schedule before enabling dispatch for a large database.
4. Create separate `tm_user_service` and `tm_control_service` login roles, neither owning tables. Apply `deploy/fleet-service-grants.sql` as the database owner. The user service cannot read worker credentials; the control service cannot read user password hashes or update portfolio ownership/notes. Reapply/review grants after future schema additions. Keep migration-owner credentials out of worker/service environments.
5. Prepare `/etc/tmtracker/control.env` and `/etc/tmtracker/user-fleet.env` with restrictive permissions. Use `/var/lib/tmtracker/fleet-artifacts` for shared storage. Install `tmtracker-control.service` and `tmtracker-user-fleet.service` on staging. The latter replaces the old user backend; do not start two processes on 5005. The control process starts its dispatcher explicitly and does not migrate at startup.
6. Merge `nginx-fleet.locations.conf` into the existing HTTPS server block. Keep TLS certificates/redirects and existing authenticated document routes intact. `/admin/` and `/api/control/` go to 5010; user `/api/` goes to 5005. Run `nginx -t` before reloading. Do not deploy the old plain-HTTP template over an HTTPS site.
7. Drain old fleet work and saved results, then stop its scheduler/worker processes. Enable the new feature flag only after this drain. New mode disables legacy fleet APIs, startup fetching resets and competing user-side fetch workers. Additions, CSV imports and refresh/retry routes write durable handoffs. Only one scheduling mode may own portfolio fetching.
8. Register one pilot machine in administration; copy its one-time token into that machine's worker env. Add an encrypted portal account and assign it to the machine. Install/verify its WireGuard tunnel and set `WORKER_MODE=live` with `WORKER_VPN_CONFIG` matching the configured name. Run the agent using the privileges needed to manage that tunnel. Workers have no production DB/Drive credentials.
9. Test actual authentication/CAPTCHA, PDF extraction, every panel, VPN disconnect, Contabo reconnect, upload, portfolio visibility and document downloads. Measure per-item duration before changing the default five-item/20-minute batch and 30-minute reservation. Bring up a second worker and test recovery/late results, then expand to 5–6.

Before the VPN portion of the pilot, follow [VPN-ROUTING-PLAN.md](VPN-ROUTING-PLAN.md). The copied configs use a single portal destination route, with DNS addresses outside that range. Prepare a reviewed per-worker config and verify portal IPv4/IPv6 destinations, DNS and coordinator HTTPS with the tunnel active. Testing production PostgreSQL on port 5432 is unnecessary: workers use `/api/control/v1/health` and authenticated coordinator APIs. `WORKER_NETWORK_MODE=offline` remains the default. Optional `split` mode sends heartbeats and uploads saved results while routing/HTTPS checks pass, then disconnects at the bounded recovery point. The copied VPN exports and running demo configuration have not been switched to live split mode.

The copied workspace has not been deployed to Contabo. Linux units/nginx, real portal/VPN behavior, restore rehearsal and multi-machine live throughput still require staging/pilot verification.

## Remaining rollout work

The portfolio fetch path is migrated. Existing ad hoc app-number/wordmark search and portal-dependent discovery/watch screens still use the legacy scraper protocol; journal parsing remains a distinct server task. Keep that service isolated for these features until their protocols and permissions are migrated; do not interpret the new portfolio flag as complete removal of the old scraper. The current worker runs one assigned portal account at a time; automatic account/VPN rotation and cooldown tuning require the live pilot. Storage retention cleanup, unified admin sign-in, detailed portal/internet diagnostics, subscription-specific shared refresh requirements and external alert delivery remain follow-up work. The current admin panel uses a scoped control credential plus the existing user-admin login. PDF-only re-extraction is now available from a saved worker result after its refresh is acknowledged and no newer refresh is pending.

## Correctness rollout checks

Apply migrations through `012` as the owner and reapply `fleet-service-grants.sql` before starting the new services. They preserve legacy source rows/status history and seed older notices as review-needed; they do not manufacture due dates. Canonical field rows supersede newest-document selection for fleet publications. No production data has been migrated during development.

`WORKER_FULL_CHECK_DAYS` defaults to 30 (allowed range 1–365). Metadata-matched/checksummed documents can be reused locally within this interval; another worker lacking those bytes downloads them. Uploaded-document fallback identity uses description/date/duplicate ordinal because stable encrypted portal locators have not been verified. A changed locator alone does not force download; forced-full and periodic checks catch replacements. In administration, select a non-reserved job and choose **Force full download**. The worker always reads lists/details/proceedings regardless of status.

Optional scan OCR uses Tesseract installed on the worker. Set `WORKER_OCR_EXECUTABLE` to its executable (or put it on PATH), and `WORKER_OCR_LANGUAGE` to installed language data (default `eng`). Rendering uses pdfplumber's existing PDF rendering dependency. OCR is limited to 20 pages/60 seconds per page; overall text extraction to 100 pages. OCR-derived events/fields require review. Without OCR, scans stay saved/review-needed and can be re-extracted later without portal/VPN access.

Test the saved notice's two-calendar-month receipt period, month-end/leap-year handling, misleading acceptance/hearing titles, actual hearing date extraction, independent opposition IDs, outgoing proceeding membership, private receipt/status isolation, repeated publication, changed-source confirmation invalidation, large-portfolio pagination and source document access. Missing dates remain visible for review and are omitted from dated digests. Additional legal periods are not guessed or enabled as implicit rules.

## Rollback

Pause/drain new workers and let pending local uploads finish. Stop the control dispatcher and new user service before re-enabling legacy scheduling. Retain new schema, artifacts and worker outboxes for reconciliation. Restore reviewed old service/env/nginx configurations and set `TM_FLEET_CONTROL_ENABLED=0` only when the new dispatcher is stopped. Do not drop tables or erase saved results. If database restoration is needed, restore into a new database first and validate it before redirecting the application.

For the route-performance/claim-ordering correction, deploy the updated coordinator before workers: workers require its Boolean `repair_only` claim filter. This correction needs no database migration. Local config/DNS preflight failures consume no scraping attempts; tunnel startup or portal failures after successful preflight retain the existing reservation/retry policy. Details and read-only timings are in [VPN-ROUTING-PLAN.md](VPN-ROUTING-PLAN.md).
