# Contabo copy-paste deployment instructions

This package starts a private staging deployment beside the current website.
The existing live database, application services, documents and public Nginx/TLS
routes remain in place. It does **not** make the updated application public yet.

The remaining production prerequisites are verified Google Drive delivery and
downloads for new fleet artifacts, complete central worker diagnostics, a real
VPN pilot and successful testing on this PostgreSQL 16 staging restore. No final
public cutover command is provided until those requirements are met. The staging
entrypoint deliberately refuses the production database.

## 1. Windows PowerShell — create and upload the release

```powershell
cd H:\Datascraper-updates
python deploy\build_contabo_bundle.py
if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed; stop here.' }
scp .runtime\contabo-staging.tar.gz .runtime\contabo-staging.tar.gz.sha256 root@62.146.239.158:/root/
if ($LASTEXITCODE -ne 0) { throw 'Upload failed; stop here.' }
```

The archive contains selected source code, migrations and deployment scripts.
It excludes `.env`, private OAuth exports, VPN configs, customer documents,
database dumps, node_modules and historical deployment notes.

## 2. Contabo terminal — install and start staging

Run in the root SSH terminal, not PowerShell:

```bash
set -e
umask 077
cd /root
sha256sum -c contabo-staging.tar.gz.sha256
install -m 0755 -d /opt/tmtracker-releases
release_dir=$(mktemp -d /opt/tmtracker-releases/staging.XXXXXX)
chmod 0755 "$release_dir"
tar -xzf /root/contabo-staging.tar.gz -C "$release_dir"
bash "$release_dir/deploy/containers/install-docker-ubuntu.sh"
bash "$release_dir/deploy/containers/server-stage.sh"
```

The scripts:

1. Check archive contents and the audited Ubuntu/PostgreSQL versions.
2. Create a private compressed database backup and configuration backup, including
   existing Nginx/TLS settings and application env files. Documents are not copied
   or deleted. Available disk is checked before creating the staging restore.
3. Restore to **`fleet_staging_datascraper`**, not `datascraper`.
4. Generate separate staging owner/control/user logins and private service env
   files automatically. Existing staging names/configuration are never overwritten.
5. Build the image, explicitly run twelve migrations, and compare actual original
   row counts/fingerprints for users, portfolios, documents, subscriptions and
   compliance data. Any discrepancy stops the rollout.
6. Apply restricted runtime grants and start loopback-only services on 5510/5505.
   Legacy background scraping, outgoing digest mail and calls to the live scraper
   are disabled in staging.
7. Check authenticated coordinator DB access and user API readiness. Add a
   separate loopback-only Nginx staging listener on **5585** and validate Nginx
   before reloading it. Existing public site files are not replaced.

A failure stops the script. **Do not proceed to public cutover or delete anything.**
Keep the output and report the failed step. Credentials are never printed.

If building fails after preparation, resume on Contabo using:

```bash
set -e
release_dir=$(python3 -c 'import json; print(json.load(open("/etc/tmtracker/staging-state.json"))["release"])')
bash "$release_dir/deploy/containers/server-stage.sh"
```

This reuses completed staging preparation. An incomplete preparation requires
review; it is not automatically erased or restored over.

## 3. Windows PowerShell — open the private staging website

Open a **second PowerShell window** and leave this command running:

```powershell
ssh -o ExitOnForwardFailure=yes -N -L 5585:127.0.0.1:5585 root@62.146.239.158
```

Open these URLs in your browser:

- Portfolio: `http://127.0.0.1:5585/`
- Administration: `http://127.0.0.1:5585/admin/`

Sign in with your existing administrator email/password from the restored copy.
The central dashboard now uses normal sign-in; do not paste a service token into
it. A normal user account has portfolio access and is denied fleet administration.

Closing the tunnel disconnects your browser from staging. It does not stop the
server or change the public site.

## 4. Contabo — view status or stop staging

Status (without printing environment credentials):

```bash
release_dir=$(python3 -c 'import json; print(json.load(open("/etc/tmtracker/staging-state.json"))["release"])')
cd "$release_dir"
docker compose --env-file /etc/tmtracker/staging-compose.env -f deploy/containers/compose.staging.yaml ps
```

Stop the staging containers:

```bash
release_dir=$(python3 -c 'import json; print(json.load(open("/etc/tmtracker/staging-state.json"))["release"])')
cd "$release_dir"
docker compose --env-file /etc/tmtracker/staging-compose.env -f deploy/containers/compose.staging.yaml down
```

This leaves the backup, staging database and artifacts intact. Do not run
`docker system prune`, delete volumes or remove `data/ipindia`.

## 5. Windows PowerShell — copy the private backups off the server

After staging preparation, use another PowerShell window:

```powershell
cd H:\Datascraper-updates
python -c "import sys; sys.path.insert(0,'deploy'); from drive_account_repair import private_folder; from pathlib import Path; private_folder(Path('.runtime/private-backups'))"
if ($LASTEXITCODE -ne 0) { throw 'Private backup folder setup failed; stop.' }
scp -r root@62.146.239.158:/root/tmtracker-backups .runtime\private-backups\
if ($LASTEXITCODE -ne 0) { throw 'Backup copy failed.' }
```

These backups contain real data and secrets. Keep them private; never paste their
contents into chat or include them in a release archive. A full off-server backup
of unverified document originals is still needed before any future cleanup.

## Public go-live gate

After the remaining code and acceptance checks pass, create a new tested
production release with a production-specific entrypoint, fresh backup,
production role/env configuration, reviewed migration, one scheduler per work
stream, and Nginx cutover/rollback commands. The current staging image refuses
`datascraper`; changing its name checks is not a deployment procedure.

Docker installation follows the [official Ubuntu apt repository method](https://docs.docker.com/engine/install/ubuntu/).
