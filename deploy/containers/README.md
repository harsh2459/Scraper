# Contabo staging containers

For the new production deployment, use [PRODUCTION.md](PRODUCTION.md).
It deploys code with local storage first; Google Drive is a later step.
The production image, live migration, TLS route change and real scrape still
require server execution and verification. The commands below describe staging.

Status: the user has built and run staging on Contabo. The coordinator readiness,
user authentication boundary and loopback staging UI checks passed there.
Docker Engine is unavailable locally.
Do not change live Nginx routes or stop services using this configuration.

The private server schema restored on local PostgreSQL 18.6. All twelve migrations
passed, preserving original synthetic rows and Drive references, and passed a
second unchanged run. Report: `.runtime/contabo-migration-rehearsal.json`.
Two simulated coordinator deliveries also passed against the restored schema,
including duplicate delivery acknowledgment, stable extraction rows and
preservation of historical Drive IDs. Three staging guard tests and Compose
configuration validation passed. These checks do not validate an image build.
Production is PostgreSQL 16.15; a PostgreSQL 16 staging restore containing real
data is still required. The public-only export omits the `pg_trgm` extension
declaration although exported indexes require it; local rehearsal bootstraps it.

## Configuration

- One image builds the user/admin frontends and runs either backend. Windows
  workers and WireGuard remain native.
- Linux host networking reaches host PostgreSQL. APIs bind to loopback on
  coordinator port 5510 and user port 5505; no `ports` mapping is used.
- The staging entrypoint refuses the live database and remote DB hosts. All
  targets must be named `fleet_staging_*` on `127.0.0.1`.
- Migrations run only through an explicit profile, using a separate owner role.
- Existing data/Journal are mounted read-only at their original paths. Staging
  artifacts and caches use separate persistent directories.
- Containers use the host tmtracker UID/GID, drop capabilities and have read-only
  root filesystems. Do not recursively chown existing application files.
- The build context excludes runtime exports, env files, VPN configs, documents,
  dumps, node_modules and historical deployment notes.

References: [Docker host networking](https://docs.docker.com/engine/network/drivers/host/),
[Docker build contexts](https://docs.docker.com/build/concepts/context/).

## Preconditions for server staging

1. Finish verified Drive delivery and central telemetry. Normal operator sign-in
   now relays authorized administrator requests through the user backend; the
   coordinator credential stays server-side. Production routing must send
   `/api/control/v1/admin/` to that authenticated relay.
   These files are preparation, not a production release.
2. Take a private compressed full backup and restore to a separate
   `fleet_staging_*` database on PostgreSQL 16. Retain the live database.
3. Create distinct staging migration/control/user DB roles with reviewed grants.
   Runtime roles must not own the database or use the postgres login.
4. Prepare private `/etc/tmtracker/staging-{migration,control,user}.env` files
   using the example, URL-encoded passwords and reviewed app settings. Put the
   actual host `TMTRACKER_UID` and `TMTRACKER_GID` in a separate
   `/etc/tmtracker/staging-compose.env` file. Protect these files with mode 600.
5. Create staging artifact/cache directories owned by that UID/GID. Confirm free
   disk space and unused staging ports. Install Docker using official Ubuntu
   instructions when this package is ready for server testing.

After those preconditions, controlled staging commands are:

```bash
docker compose --env-file /etc/tmtracker/staging-compose.env -f deploy/containers/compose.staging.yaml config --quiet
docker compose --env-file /etc/tmtracker/staging-compose.env -f deploy/containers/compose.staging.yaml build
docker compose --env-file /etc/tmtracker/staging-compose.env -f deploy/containers/compose.staging.yaml --profile migration run --rm migrate
docker compose --env-file /etc/tmtracker/staging-compose.env -f deploy/containers/compose.staging.yaml up -d control user
```

Do not print expanded `docker compose config` with real credentials; use
`--quiet`. Shutdown uses the same Compose file and project with `down`; this does
not revert staging migrations. Do not delete bind directories as a rollback.
No automatic production deployment, private credential transfer, document
cleanup or Nginx replacement is included.
