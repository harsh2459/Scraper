# Upgrade the existing Contabo application while preserving its data

## Confirmed live data, 5 October 2026

The read-only server audit has now been received and reviewed. See
[CONTABO-AUDIT-FINDINGS.md](CONTABO-AUDIT-FINDINGS.md) for the current findings,
updated document counts, service state and initial schema comparison. Docker is
absent, the new fleet schema is absent, and the legacy scraper was in an
auto-restart state. The audit supersedes the older screenshot counts below.
The private schema-only export has also been received. Its actual defaults,
indexes and constraints were restored into a new local test database; all twelve
fleet migrations passed, including synthetic data preservation and a second
unchanged run. Local PostgreSQL is 18.6 versus production 16.15, and no real
customer rows were used. A PostgreSQL 16 staging restore is still required.
Staging-only container configuration is prepared under `deploy/containers`;
it has not been built or run with Docker Engine and is not a production release.

The user has server access; the coding agent does not. The existing code is still deployed. Server screenshots establish PostgreSQL 16 with database `datascraper` at 1,831 MB, a 96 GB root filesystem with about 55 GB used, `/opt/tmtracker` at 49 GB and its `data/ipindia` directory at 47 GB. The Journal directory is approximately 1.5 GB. These are observations, not an independently collected database backup or schema audit.

The document query returned 21,441 TM-A records, 809 reminders and 7,347 correspondence records with stored local paths. Of those, 20,469 had Drive IDs with no cleanup marker and 9,128 had no Drive ID. The upload dry run found about 1.1 GB of existing candidate files; the cleanup dry run reported counts, not bytes. A limited cleanup deleted zero files because all thirty verification attempts failed. The sampled saved ID returned Drive HTTP 404.

The user opened the sampled file with `chatgptbmaemp369@gmail.com` and completed browser authorization for that account. A private authorization bundle exists on the PC. It has not been applied to Contabo. Both local copies' existing app credentials authenticate as a different account. Preserve all saved Drive IDs and remaining local originals until the server's access has been repaired and verified.

Historical deployment notes describe native systemd services for the scraper, PDF parser and user backend, host PostgreSQL and host Nginx serving the built frontend. The actual unit state, live schema and routing still require the read-only audit below. Historical documents can contain credentials: do not include them in a container image or public release archive.

## Recommended deployment boundary

Use Docker Compose for the new user backend and fleet coordinator. Keep the existing PostgreSQL 16 database and existing Nginx/TLS setup on the host for this rollout. Keep the Windows fleet worker and WireGuard native on each worker. Docker is an application-packaging decision; it neither verifies Drive storage nor migrates existing customer data automatically.

A Linux-host networking configuration is a candidate for reaching the currently loopback-bound database and publishing loopback-only application listeners. Validate the host configuration before selecting it. Do not assume `127.0.0.1` inside a default bridged container refers to the host database. Container paths for legacy documents must preserve existing absolute paths or have an explicitly tested mapping. Persist the coordinator's unacknowledged artifacts outside disposable container layers.

Do not move the database into a fresh container volume, replace its data directory, expose port 5432 publicly, copy 47 GB into an image or rebuild Nginx over the live certificate-bearing configuration. Keep the same database identities, users, portfolios, subscriptions, documents and saved Drive references. Application images must exclude all `.env` files, private authorization exports, VPN configs, databases/dumps, documents, caches, virtual environments and historical deployment credentials.

## Read-only inventory before implementation against the live schema

`inspect_contabo.py` uses only the system Python standard library. It does not import application modules. It reports database schema metadata, estimated row counts, sizes, aggregate Drive counts, service state, selected Nginx routes and presence of configuration keys. It omits passwords, tokens, private keys, customer records and file IDs. Database queries run in read-only transactions with statement timeouts. No server configuration changes or cleanup are performed.

From the PC:

```powershell
cd H:\Datascraper-updates
scp deploy\inspect_contabo.py root@62.146.239.158:/root/inspect_contabo.py
```

On Contabo:

```bash
umask 077
python3 /root/inspect_contabo.py --root /opt/tmtracker --database datascraper > /root/contabo-audit.json
```

The output report is the input for schema compatibility and deployment preparation. It may take up to two minutes to count application-folder sizes. No upload/deletion/migration scripts are started by the audit. Estimated PostgreSQL counts are not exact customer-record counts.

## Required code work before a production release

1. Compare the audited schema with the current user backend and additive fleet migrations. Prepare and test a migration from a restored copy of the real database. Do not replace it with the demo database or import synthetic demo records. Preserve existing primary keys, user ownership and Drive references.
2. Complete the new fleet's Drive integration. Current coordinator uploads use local artifact storage; new files need a verified Drive copy and durable Drive-ID mapping before local staging cleanup. Both administrative and portfolio downloads must use the same authorization rules when retrieving Drive-backed artifacts. Drive failures must retain the local outbox/staging copy, and retries/lost acknowledgments must not create uncontrolled duplicate uploads.
3. Add key-free worker VPN inventory and operational diagnostics to central telemetry, then present them in the live administration dashboard. Worker authentication is automatic after provisioning; VPN private keys remain on their owning worker.
4. Fleet operator access now uses the existing administrator sign-in through the user backend's local coordinator relay. The service credential remains server-side; real administrator/session checks and mutation origin checks were tested. Production Nginx must route the administrative API to the relay. Worker service authentication remains separate. Verify the full sign-in flow in staging before cutover.
5. Build the server images and Compose configuration, private environment templates, backup/restore instructions and staged deployment/rollback scripts for the audited paths and ports. Containers run as an application user, never as a privileged VPN container.

The Drive integration, full central telemetry, unified operator sign-in and Docker release are required work, not completed features. The existing ninety-six application tests and six authorization-helper tests do not establish those unimplemented paths.

## Staging and rollout

1. Create a verified PostgreSQL backup and a separate staging restore. Back up the current configuration privately. The available disk cannot hold an uncompressed second copy of the entire 47 GB document tree; use an off-server backup or verified remote document coverage rather than assuming another local full copy fits.
2. Test migration against the staging restore, including unchanged user/document identities and readable historical documents. Compare actual row counts, schema constraints and representative portfolio views before and after. Exercise new delivery, Drive outage/retry, checksum verification, old-file reads and local-file fallback.
3. Apply the correct Drive authorization on Contabo as part of the deployment. Verify the previously failing ID, configured Drive folder and actual application downloads. A successful Google sign-in alone is insufficient.
4. Start the new backends on separate loopback staging ports while the old site continues serving. Review the deployment and migration results before switching routing. Preserve the live Nginx certificate directives and test any additions with `nginx -t`.
5. Stop overlapping legacy/new schedulers during cutover so there is one authority for new fleet work. Enable the coordinator, provision one real Windows worker, and perform the active-VPN/Contabo acceptance pilot before expanding the fleet.
6. Keep the previous application release and explicit database rollback procedure. Additive changes are preferred, but do not assume old code tolerates every new publication or that image rollback reverses schema changes.
7. Resume bounded, checksum-verified cleanup only after historical documents and new Drive-backed deliveries are confirmed readable. Do not bulk-delete `data/ipindia` or assume every file in it is represented by a successfully uploaded database row.

Reference: Docker's [production Compose guidance](https://docs.docker.com/compose/how-tos/production/) and [persistent storage documentation](https://docs.docker.com/engine/storage/volumes/).
