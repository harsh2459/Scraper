# TM Tracker — How everything is set up + how to push code changes

Read this first, then hand it to AI when you want a change made.

---

## THE LIVE SETUP (as of first deploy)

**VPS:** Contabo Cloud VPS 4 · IP `62.146.239.158` · Singapore · Ubuntu 24.04
**SSH:** `ssh root@62.146.239.158`  (or `root@mytmtracker.com`)
**Domain:** GoDaddy `mytmtracker.com`
  - DNS A records: `@`, `www`, `scraper` → all `62.146.239.158`

### Live URLs (HTTPS, Let's Encrypt, auto-renew)

| URL | App | What it does |
|---|---|---|
| `https://mytmtracker.com`        | **Backend + Frontend** (TM Manager) | portfolio, leads, invoices, status timeline, journal watch |
| `https://scraper.mytmtracker.com`| **Scraper** (its own Flask UI)      | run IP India scrapes, OTP email pool (Settings), Auto Discover, scrape history |

**Login for both:** `admin` / `Nvgalani@369`
- `mytmtracker.com` uses a real branded login page (session cookie, "keep me signed in") as of 2026-09-08.
- `scraper.mytmtracker.com` still uses the browser's Basic-auth popup.

### On the server — the 4 components

| # | Component | Path | Runs as | Port (localhost) |
|---|---|---|---|---|
| 1 | **Scraper** (IP India engine) | `/opt/tmtracker/` (root files) | systemd `tmtracker-scraper` | 5001 |
| 2 | **PDF Scraper** (saves/parses PDFs handed off by #1 — never talks to IP India itself) | `/opt/tmtracker/pdf_scraper/` | systemd `tmtracker-pdf-scraper` | 5002 (127.0.0.1 only, no nginx route) |
| 3 | **Backend** (TM Manager API) | `/opt/tmtracker/tm_manager/` | systemd `tmtracker-backend`  | 5005 |
| 4 | **Frontend** (built React) | `/opt/tmtracker/tm_manager/frontend/dist/` | nginx serves it | 80/443 |

The PDF Scraper is purely internal — only the Scraper (#1) ever calls it, over `127.0.0.1:5002`. It's never exposed via nginx and doesn't need to be reachable from outside the server. If it's down, the Scraper falls back to saving/parsing PDFs itself (old behavior) so no document is ever lost — but restart it as soon as convenient.

- **Database:** PostgreSQL 16, database `datascraper`, user `tmtracker` / `Nvgalani@369`. Both #1 and #2 share it.
- **Python:** one venv at `/opt/tmtracker/.venv` — both #1 and #2 use it.
- **nginx:** `/etc/nginx/sites-available/tmtracker` — routes `/api/*`→5005, `/scraper-api/*`→5001, `scraper.` subdomain→5001, everything else→the static frontend.
- **App user:** `tmtracker` (locked-down; never run the apps as root).
- **Config files (NOT in git, live only on the server):**
  - `/opt/tmtracker/.env`            — scraper settings (DB pw, OTP Gmail, BASIC_AUTH, USE_WAITRESS=1)
  - `/opt/tmtracker/tm_manager/.env` — backend settings (DB pw, OPS_API_USER/PASS for monitoring, USE_WAITRESS=1)
- **Logs:**
  - `sudo journalctl -u tmtracker-scraper -f`
  - `sudo journalctl -u tmtracker-pdf-scraper -f`
  - `sudo journalctl -u tmtracker-backend -f`
- **Data folder:** `/opt/tmtracker/data/ipindia/` — 20,381 application folders, ~38 GB of PDFs/logos. Restored from local. Disk: 96 GB total, ~45% used.

### What was already migrated

- DB: full local `datascraper` restored (trademarks, leads, invoices, ipindia_results, status history).
- `data/` folder: full 38 GB uploaded + unpacked.
- 22 performance indexes built.
- `tm_status_history` backfilled from existing scrapes.

---

## MULTI-TENANT MIGRATION (one-time, do this before the old shared login stops working)

TM Manager moved from one shared login to real per-user accounts. Each signed-up
user only sees their own trademarks; the admin account still sees everyone's.
**Do this in order, on the server, before/during deploying the new code:**

1. Upload the new `tm_manager/app.py`, `tm_manager/db.py`, `tm_manager/config.py`,
   `tm_manager/scripts/create_admin_and_backfill.py`, and the rebuilt frontend.
2. **Before restarting the backend**, create your admin account and backfill every
   existing trademark to it:
   ```
   cd /opt/tmtracker/tm_manager
   sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/create_admin_and_backfill.py you@example.com "a real password"
   ```
   Confirm it prints `my_trademarks rows still with no owner: 0` before continuing —
   do not proceed if that number isn't 0.
3. Restart the backend: `sudo systemctl restart tmtracker-backend`. This is the
   moment the schema locks (`user_id` becomes required, the uniqueness constraint
   changes) AND the old shared password (`BASIC_AUTH_USER`/`PASS`) stops working as
   a login — you'll need to sign in with the email/password from step 2 instead.
4. Rebuild the frontend if not already done: `cd frontend && sudo -u tmtracker npm run build`.
5. Log in at `https://mytmtracker.com/login` with your new admin account and confirm
   the dashboard still shows every existing trademark.
6. Add `OPS_API_USER`/`OPS_API_PASS` to `/opt/tmtracker/tm_manager/.env` if you want a
   separate credential for monitoring curl commands below — otherwise the old
   `BASIC_AUTH_USER`/`PASS` values keep working for that purpose only (not as a login).
7. To add a teammate/customer account later, run the same script again with a
   different email — it just creates that account (only the very first run's account
   needs the backfill).

---

## HOW TO PUSH A CODE CHANGE

There are 3 files/areas you might change. Each has its own "apply" step.

### The workflow (every time)

1. **AI edits the files** on your Windows PC (`H:\Datascraper\...`).
2. **You upload the changed file(s)** to the server — one of:
   - **WinSCP** (drag the file into the same path on the server), or
   - **Git Bash:** `scp /h/Datascraper/<path> root@mytmtracker.com:/opt/tmtracker/<path>`
     (enter the Contabo root password)
3. **You run ONE apply command** on the server (SSH, or WinSCP's terminal tab).

### Which apply command?

| You changed... | Files | Apply command (run on the server) |
|---|---|---|
| **Scraper Python** | `app.py`, `ipindia_scraper.py`, `warm_pool.py`, `email_otp.py`, `config.py`, `db.py`, `trademark_scraper_core.py`, `folder_export.py`, `ipindia_grouping.py`, `scripts/*.py` | `sudo systemctl restart tmtracker-scraper` |
| **Scraper HTML/UI** | `templates/*.html` | `sudo systemctl restart tmtracker-scraper` |
| **PDF Scraper Python** | `pdf_scraper/*.py` | `sudo systemctl restart tmtracker-pdf-scraper` |
| **Backend Python** | `tm_manager/app.py`, `tm_manager/db.py`, `tm_manager/scheduler.py`, `tm_manager/tm_leads_db.py`, `tm_manager/tm_leads_routes.py`, `tm_manager/config.py`, `tm_manager/scripts/*.py` | `sudo systemctl restart tmtracker-backend` |
| **Frontend (React)** | anything under `tm_manager/frontend/src/**`, `index.html`, `vite.config.ts`, `package.json` | `cd /opt/tmtracker/tm_manager/frontend && sudo -u tmtracker npm run build` |
| **A `.env` file** | `/opt/tmtracker/.env` or `/opt/tmtracker/tm_manager/.env` | `sudo systemctl restart tmtracker-scraper tmtracker-pdf-scraper tmtracker-backend` |
| **nginx location block (routing)** | edit `/etc/nginx/sites-available/tmtracker` **directly on the server** with `sudo nano`. ⚠️ **NEVER** re-run the `sed .../deploy/nginx-tmtracker.conf > sites-available/tmtracker` copy on a live server — that file has no `listen 443 ssl` / cert lines, so it **deletes HTTPS** and the site goes `ERR_CONNECTION_REFUSED` until certbot is re-run (happened twice: 2026-09-08, 2026-09-11). `deploy/nginx-tmtracker.conf` in the repo is a reference/first-boot-only template, kept in sync manually — it is NOT meant to be deployed over a cert-bearing config. | `sudo nginx -t && sudo systemctl reload nginx` — and if you ever DO overwrite it by mistake: `sudo certbot --nginx -d mytmtracker.com -d www.mytmtracker.com -d scraper.mytmtracker.com --redirect --non-interactive` |
| **A new Python dependency** | `requirements.txt` or `tm_manager/requirements.txt` | `sudo -u tmtracker /opt/tmtracker/.venv/bin/pip install -r /opt/tmtracker/requirements.txt -r /opt/tmtracker/tm_manager/requirements.txt` then restart the relevant service |
| **A DB schema change** (new table/column added in a `db.py` schema block) | `db.py` / `tm_manager/db.py` | restart the service — `get_connection()` runs `CREATE TABLE IF NOT EXISTS` on first call. For indexes: `sudo -u tmtracker /opt/tmtracker/.venv/bin/python /opt/tmtracker/scripts/apply_perf_indexes.py` |

### If you changed a LOT of files — just re-upload everything and run deploy.sh

**Windows (Git Bash):**
```
cd /h/Datascraper
tar --exclude=node_modules --exclude=dist --exclude=.venv --exclude=__pycache__ --exclude=data --exclude='*.log' --exclude='_run_*' --exclude='*.db' --exclude='*.dump' --exclude='*.sql' --exclude=.env --exclude='tm_manager/.env' --exclude=.git --exclude=.claude --exclude=scratch --exclude=video-software -czf /h/Datascraper/update.tar.gz .
scp /h/Datascraper/update.tar.gz root@mytmtracker.com:/opt/tmtracker/
```
**Server:**
```
cd /opt/tmtracker
tar -xzf update.tar.gz && rm update.tar.gz
chown -R tmtracker:tmtracker /opt/tmtracker
sudo -u tmtracker /opt/tmtracker/.venv/bin/pip install -q -r requirements.txt -r tm_manager/requirements.txt
sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/apply_perf_indexes.py
cd tm_manager/frontend && sudo -u tmtracker npm run build && cd ../..
sudo systemctl restart tmtracker-scraper tmtracker-pdf-scraper tmtracker-backend
sudo systemctl reload nginx
```

### One-time: Google Drive document storage credentials

Documents/PDFs are now uploaded to Google Drive instead of staying on
server disk (falls back to local disk automatically if Drive is
unreachable — nothing breaks if this step is skipped, it just means new
documents keep saving locally like before). `.env` files are excluded
from the tarball on purpose (see above), so this one credential-apply
step has to run once, by hand, after the code above is deployed:

```
sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/apply_google_drive_env.py \
    --client-id "..." --client-secret "..." --refresh-token "..." --root-folder-id "..."
sudo systemctl restart tmtracker-scraper tmtracker-pdf-scraper tmtracker-backend
```

The four values come from running `scripts/google_drive_auth_setup.py`
locally (one-time interactive Google login) — ask AI for them if you
don't have them handy. The script only adds these four lines; it never
touches or overwrites any other line already in either `.env` file, and
running it again later is harmless (skips keys that already exist).

To later free up the 46 GB already on disk, run the backfill script
(safe to run anytime, throttled, never runs automatically):
```
sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/backfill_drive_migration.py --pass upload --dry-run
sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/backfill_drive_migration.py --pass upload
# review results, then once satisfied:
sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/backfill_drive_migration.py --pass delete --dry-run
sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/backfill_drive_migration.py --pass delete
```

---

## WHAT TO TELL AI

Paste this whole file, then say what you want changed, e.g.:

> "Read deploy/HOW-TO-UPDATE.md. I want to <change X>. Make the edit, tell me
>  exactly which file(s) to upload and the one apply command to run on the server."

AI should respond with:
1. the edited file(s) (done in `H:\Datascraper\...`)
2. the `scp` line(s) OR "drag these in WinSCP: <paths>"
3. the single apply command from the table above

---

## COMMON OPS (no code change)

| Task | Command (on the server) |
|---|---|
| Restart everything | `sudo systemctl restart tmtracker-scraper tmtracker-pdf-scraper tmtracker-backend && sudo systemctl reload nginx` |
| Is it running? | `sudo systemctl status tmtracker-scraper tmtracker-backend` |
| Watch scraper log | `sudo journalctl -u tmtracker-scraper -f`  — or raw stdout/stderr: `sudo tail -f /var/log/tmtracker/scraper.err` |
| Watch backend log | `sudo journalctl -u tmtracker-backend -f`  — or: `sudo tail -f /var/log/tmtracker/backend.err` (this is where Python tracebacks actually show up, not always journalctl) |
| Edit a setting | `sudo nano /opt/tmtracker/.env` (or `tm_manager/.env`) → save → `sudo systemctl restart tmtracker-scraper tmtracker-pdf-scraper tmtracker-backend` |
| Add a warm-pool Gmail (fast fetching) | `sudo -u tmtracker /opt/tmtracker/.venv/bin/python /opt/tmtracker/scripts/pool_accounts.py add you@gmail.com "app pw" --role warm` then `sudo systemctl restart tmtracker-scraper` |
| Move an existing account to warm/cold | `sudo -u tmtracker /opt/tmtracker/.venv/bin/python /opt/tmtracker/scripts/pool_accounts.py set-role you@gmail.com warm` then `sudo systemctl restart tmtracker-scraper` |
| List OTP pool accounts | `sudo -u tmtracker /opt/tmtracker/.venv/bin/python /opt/tmtracker/scripts/pool_accounts.py list` |
| Check auto-fetch is enabled / worker count | `grep AUTO_FETCH /opt/tmtracker/tm_manager/.env` — as of 2026-09-08, `AUTO_FETCH_ENABLED` defaults to **on** in code (`scheduler.py`), only needs a `.env` line if you want it OFF (`AUTO_FETCH_ENABLED=0`) or a specific worker count (`AUTO_FETCH_WORKERS=N` — keep N ≤ number of warm accounts) |
| **One-glance fetch-queue health** (pending/fetching/fetched counts, whether the worker is alive, warm-pool status) | `curl -s -u admin:Nvgalani@369 https://mytmtracker.com/api/fetch-queue/status` |
| Is the backlog actually moving? | Run `SELECT status, count(*) FROM my_trademarks GROUP BY status;` (below), wait 2 min, run again — `pending` should be dropping |
| Renew HTTPS (auto, but to test) | `sudo certbot renew --dry-run` |
| **Recover HTTPS if it ever breaks** (`ERR_CONNECTION_REFUSED`, `:443` not listening) | `sudo certbot --nginx -d mytmtracker.com -d www.mytmtracker.com -d scraper.mytmtracker.com --redirect --non-interactive` — see the nginx warning above for why this happens |
| DB console | `sudo -u tmtracker psql -h localhost -U tmtracker -d datascraper`  (pw `Nvgalani@369`) |
| Disk usage | `df -h /` |

### Debugging checklist when something 500s or the queue is stuck

1. `curl -s -u admin:Nvgalani@369 https://mytmtracker.com/api/fetch-queue/status` — tells you counts, whether auto-fetch is on, warm-pool health, and `stuck_fetching_count` (>0 for a while = the worker crashed).
2. `sudo tail -80 /var/log/tmtracker/backend.err` — the actual Python traceback. `journalctl` sometimes doesn't show it if output is being redirected straight to these files instead.
3. **`AttributeError: module 'db' has no attribute 'X'`** → the `db.py` on the server is older than `app.py`/`scheduler.py`. This happened on 2026-09-11 — a partial upload left a stale `db.py` while `app.py` already called a function that didn't exist yet. Fix: re-upload **all three** of `app.py`, `db.py`, `scheduler.py` together, never just one — they're versioned as a set. Then `sudo systemctl restart tmtracker-backend`.
4. Confirm the fix landed: `grep -c "bump_trademark_priority" /opt/tmtracker/tm_manager/db.py` should be ≥ 1.

---

## FLEET MODE (moving scraping onto local worker machines)

Fully built already (`fleet_api.py`, `fleet_scheduler.py`,
`db.plan_daily_fleet_assignments`) but **off by default** — nothing changes
for the current single-machine (Contabo-only) setup until you turn this on.
No code work needed to bring a worker online, just the config below.

**Roles:**
- **Contabo stays the coordinator** — Postgres, the web app, and its own
  live/interactive scraping (TM Search, Word Mark search, Auto Discover,
  the warm-pool priority lane for Live Refresh) are untouched by this.
  Contabo only additionally plans + hands out daily batch-fetch assignments.
- **Each local machine is a worker** — runs its own copy of the scraper
  (`app.py`) and `tm_manager` (for its own `scheduler.py`, which is what
  actually polls Contabo for assignments), pointed at the SAME Postgres
  Contabo uses.

### 1. On Contabo (one-time)
Add to `/opt/tmtracker/tm_manager/.env`:
```
FLEET_COORDINATOR_ENABLED=1
```
`sudo systemctl restart tmtracker-backend`. This starts the daily planning
tick (`FLEET_PLAN_HOUR`, default hour 0) which divides due trademarks across
active machines, respecting `FLEET_PER_ACCOUNT_DAILY_CAP` (default 350/day
per OTP account) — override either in the same `.env` if needed.

### 2. On each local worker machine (per machine)
- Install the app the same way `server-setup.sh` does on Contabo (same repo,
  same `.venv`, same three systemd services) — but pointed at Contabo's
  Postgres instead of a local one: set `PG_HOST`/`PG_PORT`/`PG_USER`/
  `PG_PASSWORD` in this machine's `.env` to Contabo's DB (requires Postgres
  on Contabo to accept remote connections from this machine's IP — see
  `pg_hba.conf`/`listen_addresses`, not covered by the current single-
  machine `server-setup.sh`).
- In **both** `.env` files on this machine (`/opt/tmtracker/.env` for the
  scraper AND `/opt/tmtracker/tm_manager/.env` for the backend — the
  worker-side flag is read by both, see `config.py`'s comment on why):
  ```
  FLEET_MODE_ENABLED=1
  FLEET_MACHINE_KEY=<unique-slug-for-this-machine>   # e.g. laptop-nirav-01
  FLEET_COORDINATOR_URL=https://mytmtracker.com
  ```
  `FLEET_MACHINE_KEY` must be unique per machine and stable (never derived
  from hostname) — pick it once and don't change it.
- Assign this machine some OTP pool accounts (`assigned_machine_id` on
  `otp_email_pool`) — via `scripts/pool_accounts.py` or the Fleet dashboard,
  same as any other pool account.
- Restart all three services on this machine.

### 3. Verify
- `SELECT * FROM fleet_machines;` on Contabo's DB should show a new row
  (auto-registered on this worker's first heartbeat) with a recent
  `last_heartbeat_at`.
- Fleet dashboard (`/admin/fleet` in the web UI) should show it as active.
- After the next planning tick (or the dashboard's manual "Replan now"
  button), `fleet_assignments` should show rows assigned to this machine's
  `machine_id` — confirm with
  `SELECT count(*) FROM fleet_assignments WHERE machine_id = <id> AND work_date = current_date;`
- Watch this machine's own `tmtracker-backend` log
  (`sudo journalctl -u tmtracker-backend -f`) for its `scheduler.py` loop
  picking up and running an assigned batch.

---

## BACKUP

**Automated (runs daily at 03:30, no action needed):** `tmtracker-backup.timer`
runs `deploy/backup.sh`, which `pg_dump`s the database to
`/opt/tmtracker/backups/datascraper-<timestamp>.dump` (custom format,
restorable with `pg_restore`) and prunes anything older than 14 days.
Installed automatically by `server-setup.sh` on a fresh setup; on an
existing server, install it once with:
```
sudo cp /opt/tmtracker/deploy/tmtracker-backup.service /opt/tmtracker/deploy/tmtracker-backup.timer /etc/systemd/system/
sudo chmod +x /opt/tmtracker/deploy/backup.sh
sudo systemctl daemon-reload
sudo systemctl enable --now tmtracker-backup.timer
```
Check it's scheduled: `systemctl list-timers tmtracker-backup.timer`. Check
the last run: `sudo journalctl -u tmtracker-backup -n 20`. The backup files
still live on the same server/disk as the database — download them
periodically (WinSCP, or an offsite sync) rather than treating on-disk
backups alone as sufficient against a whole-VPS failure.

**Manual DB dump (on-demand, on the server):**
```
sudo -u tmtracker pg_dump -h localhost -U tmtracker -d datascraper --no-owner --no-privileges -f /opt/tmtracker/backup-$(date +%F).sql
```
Then download it with WinSCP and keep it somewhere safe. Useful right before
a risky migration/update, in addition to the nightly automated backup.

**data/ folder** — 38 GB, changes slowly; a monthly `tar -czf` + download is enough,
or set up Contabo's "VPS Auto Backup" add-on in the panel for whole-disk snapshots.

---

## SECURITY TODO (when you have a minute)

- `Nvgalani@369` is weak and is used for both the DB and the scraper UI's own
  Basic-auth popup (`scraper.mytmtracker.com`). TM Manager itself now uses real
  per-user accounts (see MULTI-TENANT MIGRATION above), not this shared password.
  - DB: `sudo -u postgres psql -c "ALTER USER tmtracker WITH PASSWORD 'NEW_STRONG';"`
    then edit `PG_PASSWORD` in both `/opt/tmtracker/.env` files → restart both services.
  - Scraper UI / ops credential: edit `BASIC_AUTH_PASS` (scraper) and/or
    `OPS_API_USER`/`OPS_API_PASS` (tm_manager's monitoring-only credential) in
    the relevant `.env` file(s) → restart the affected service(s).
  - Individual TM Manager accounts: log in as that user and (once a change-
    password flow exists) update their own password, or re-run
    `scripts/create_admin_and_backfill.py` with the same email to reset it.
- Consider `ufw` is already on (SSH + 80/443 only). Confirm: `sudo ufw status`.
- Set up SSH keys instead of password login (optional but recommended).
