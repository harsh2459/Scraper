# Production: code first, Google Drive later

This release targets the existing PostgreSQL 16 `datascraper` database and
`https://mytmtracker.com`. Staging uses a separate database and remains separate.
The first deployment uses local artifact storage. No OAuth file transfer or
Google Drive connection is required. Historical PDFs, Drive references, users,
portfolios and notes are retained. No bulk file deletion is included.

## Windows PowerShell: upload

```powershell
cd H:\Datascraper-updates
python deploy\build_contabo_bundle.py --production
if ($LASTEXITCODE -ne 0) { throw 'Build failed. Stop here.' }
scp .runtime\contabo-production.tar.gz .runtime\contabo-production.tar.gz.sha256 root@62.146.239.158:/root/
if ($LASTEXITCODE -ne 0) { throw 'Upload failed. Stop here.' }
ssh root@62.146.239.158
```

## Contabo root terminal: prepare

Run this in the SSH session, not Windows PowerShell. Preparation leaves the old
public application running. It validates the existing Nginx main site, rehearses
migrations on the existing separate PostgreSQL 16 staging database, builds the
production image and extracts the new frontend. Private service credentials
remain in root-only `/etc/tmtracker/live-*.env` files.

```bash
cd /root
sha256sum -c contabo-production.tar.gz.sha256 && (
  set -e
  umask 022
  install -d -m 0755 /opt/tmtracker-releases
  production_release=$(mktemp -d /opt/tmtracker-releases/production.XXXXXX)
  chmod 0755 "$production_release"
  tar -xzf /root/contabo-production.tar.gz -C "$production_release"
  chmod -R a+rX "$production_release"
  python3 "$production_release/deploy/containers/server_production.py" prepare
)
```

If preparation fails, send the error message. Do not delete the recorded state,
reset roles, replace `.env` files or manually change the live database. A failed
build/rehearsal can be retried from the recorded release using `prepare`.

## Contabo: activate after preparation passes

Activation creates a short maintenance window. It stops the old backend and
scraper/PDF services, takes a fresh private database and configuration backup,
applies versioned migrations, checks original-row fingerprints and grants the
two dedicated runtime roles their reviewed permissions. It starts the new APIs
on loopback ports 5005/5010, verifies DB/authentication readiness, switches only
the existing main site's static root and fleet routes, validates Nginx and tests
HTTPS locally with the production hostname. Certificates and old data remain.
The old services are disabled only after the new site passes these checks.

```bash
production_release=$(python3 -c 'import json; print(json.load(open("/etc/tmtracker/live-state.json"))["release"])')
python3 "$production_release/deploy/containers/server_production.py" activate
```

Open `https://mytmtracker.com`, sign in with an existing administrator and open
`https://mytmtracker.com/admin/`. Check portfolio records and a historical local
PDF. Administrator access uses normal account login; no browser admin token is
required. New fleet workers require separate provisioning and a real VPN/scrape
pilot. Legacy scraper-dependent wordmark/pool functionality needs its own
compatibility verification; readiness checks do not prove those features.

## Rollback

Failed activation attempts restore the previous routes/services automatically.
To roll back a completed deployment:

```bash
production_release=$(python3 -c 'import json; print(json.load(open("/etc/tmtracker/live-state.json"))["release"])')
python3 "$production_release/deploy/containers/server_production.py" rollback
```

Rollback retains the live database and its additive migrations. It never
silently restores an old backup over subsequent user writes. A database restore
requires a separately reviewed maintenance procedure. The backup location is
recorded privately in `/etc/tmtracker/live-state.json`.

## Later: Google Drive

After normal live usage has been checked, transfer the private authorization
bundle separately, verify `chatgptbmaemp369@gmail.com`, test upload/checksum/
download, then enable Drive storage. Files which exist only on Drive need that
connection before they can be retrieved. Keep all local copies until verified
delivery and checksum-safe cleanup are implemented and checked. Changing a
Drive account never requires wiping existing document IDs.
