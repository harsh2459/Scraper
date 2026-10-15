# Deploying to a Contabo VPS (Ubuntu 22.04 / 24.04)

Three components, one VPS:

| # | Component | Dir | Port (localhost only) | Run as |
|---|---|---|---|---|
| 1 | **Scraper** (IP India engine) | `/opt/tmtracker` root | `127.0.0.1:5001` | systemd `tmtracker-scraper` |
| 2 | **Backend** (TM Manager API) | `/opt/tmtracker/tm_manager` | `127.0.0.1:5005` | systemd `tmtracker-backend` |
| 3 | **Frontend** (static build) | `/opt/tmtracker/tm_manager/frontend/dist` | served by nginx :80/:443 | nginx |

nginx is the only thing listening on a public port. It serves the static
frontend and reverse-proxies the API paths to the two Python processes.

---

## What you upload (the whole repo minus junk)

Upload everything EXCEPT:
- `node_modules/`, `dist/`, `__pycache__/`, `.venv/`
- `*.log`, `_run_*.log`, `_run_*.err`
- `*.db` (old local SQLite — Postgres is the live DB)
- `data/` (regenerated on the server; the app writes PDFs/logos here)
- `.env`, `tm_manager/.env` (you create fresh ones on the server from the
  `.env.production.example` templates — never upload local secrets)

Everything else is needed. See **FILE MAP** at the bottom for exactly which
files belong to which of the 3 components.

---

## One-time server setup

```bash
# --- as root ---
apt update && apt install -y python3 python3-venv python3-pip postgresql nginx git

# Postgres DB + user
sudo -u postgres psql <<'SQL'
CREATE DATABASE datascraper;
CREATE USER tmtracker WITH PASSWORD 'CHANGE_ME_STRONG';
GRANT ALL PRIVILEGES ON DATABASE datascraper TO tmtracker;
ALTER DATABASE datascraper OWNER TO tmtracker;
SQL

# app user + dir
useradd -r -m -d /opt/tmtracker -s /bin/bash tmtracker
mkdir -p /opt/tmtracker && chown -R tmtracker:tmtracker /opt/tmtracker
```

Upload the repo to `/opt/tmtracker` (scp / rsync / git clone), then:

```bash
sudo -u tmtracker bash
cd /opt/tmtracker

# Python venv for BOTH scraper and backend (they share the same deps set)
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r tm_manager/requirements.txt

# env files (fill in real values)
cp deploy/.env.production.example .env
cp deploy/tm_manager.env.production.example tm_manager/.env
nano .env                 # PG_PASSWORD, OTP creds, USE_WAITRESS=1, BASIC_AUTH_*
nano tm_manager/.env      # PG_PASSWORD, USE_WAITRESS=1, BASIC_AUTH_*

# create the DB schema + performance indexes
.venv/bin/python -c "import db; db.get_connection().close()"           # scraper tables
cd tm_manager && ../.venv/bin/python -c "import db; db.get_connection().close()" && cd ..   # tm_manager tables
.venv/bin/python scripts/apply_perf_indexes.py

# build the frontend (needs Node 20+)
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -   # as root, once
apt install -y nodejs                                       # as root
cd tm_manager/frontend && npm ci && npm run build && cd ../..
exit   # back to root
```

## systemd + nginx (as root)

```bash
cp /opt/tmtracker/deploy/tmtracker-scraper.service   /etc/systemd/system/
cp /opt/tmtracker/deploy/tmtracker-backend.service   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tmtracker-scraper tmtracker-backend
systemctl status tmtracker-scraper tmtracker-backend

# nginx
cp /opt/tmtracker/deploy/nginx-tmtracker.conf /etc/nginx/sites-available/tmtracker
# edit server_name in that file to your domain or the VPS IP
ln -sf /etc/nginx/sites-available/tmtracker /etc/nginx/sites-enabled/tmtracker
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# HTTPS (if you have a domain pointed at the VPS)
apt install -y certbot python3-certbot-nginx
certbot --nginx -d your.domain.com
```

Open the firewall for 80/443 only:
```bash
ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw enable
```

## Add warm-pool email accounts (makes fetching fast)

```bash
sudo -u tmtracker /opt/tmtracker/.venv/bin/python \
  /opt/tmtracker/scripts/pool_accounts.py add you@gmail.com "app pw here" --role warm
# repeat for each account, then:
systemctl restart tmtracker-scraper
curl -s http://127.0.0.1:5001/api/warm-pool/status
```

## Migrate your existing local data (optional)

On your Windows box: `pg_dump -Fc datascraper > datascraper.dump`
On the VPS: `pg_restore --clean --if-exists -d datascraper datascraper.dump`
Then re-run `apply_perf_indexes.py` and `tm_manager/scripts/backfill_status_history.py`.

## Updating later

```bash
sudo -u tmtracker bash -c '
  cd /opt/tmtracker && git pull &&
  .venv/bin/pip install -r requirements.txt -r tm_manager/requirements.txt &&
  cd tm_manager/frontend && npm ci && npm run build'
systemctl restart tmtracker-scraper tmtracker-backend
systemctl reload nginx
```

---

## FILE MAP — which file belongs to which component

### 1. SCRAPER  (repo root — runs `python app.py` → `:5001`)
Core, all required:
- `app.py`               — Flask app, all scrape/job/warm-pool endpoints
- `config.py`            — loads `.env`, all settings
- `db.py`                — Postgres access + connection pool + `scrape_jobs`
- `ipindia_scraper.py`   — the IP India session/CAPTCHA/search engine
- `email_otp.py`         — IMAP OTP reader (poll + IDLE)
- `warm_pool.py`         — warm session pool
- `trademark_scraper_core.py`, `folder_export.py`, `ipindia_grouping.py` — helpers imported by `app.py`
- `requirements.txt`
- `scripts/pool_accounts.py`        — CLI: manage warm/cold email split
- `scripts/apply_perf_indexes.py` + `scripts/perf_indexes.sql` — one-time index build
Optional / one-off (not run by the service):
- `migrate_to_postgres.py`   — only if migrating old SQLite
- `trademark_scraper.py`, `delete_journal_2274.py` — old standalone utilities, not used by the app
Runtime data (created on server, do NOT upload): `data/`, `*.log`, `templates/` (legacy, unused — `render_template` is never called)

### 2. BACKEND  (`tm_manager/` — runs `python app.py` → `:5005`)
Core, all required:
- `tm_manager/app.py`            — Flask API (trademarks, dashboard, history, changes, journal)
- `tm_manager/config.py`         — loads `tm_manager/.env`
- `tm_manager/db.py`             — Postgres + pool + `tm_status_history` ledger
- `tm_manager/tm_leads_db.py`    — leads/invoices data (shares db.py's pool)
- `tm_manager/tm_leads_routes.py`— `/api/tm/*` blueprint
- `tm_manager/scheduler.py`      — auto-fetch + staleness + journal background workers
- `tm_manager/requirements.txt`
- `tm_manager/scripts/parse_journal.py`        — journal PDF parser (imported by app.py)
- `tm_manager/scripts/auto_fetch_journal.py`   — daily journal downloader (run by scheduler)
- `tm_manager/scripts/backfill_status_history.py` — one-time history backfill
Runtime data (created on server, do NOT upload): `tm_manager/Journal/`, `tm_manager/_*_cache/`, `tm_manager/*.log`, `tm_manager/templates/` (legacy, unused)

### 3. FRONTEND  (`tm_manager/frontend/` — `npm run build` → static `dist/`, served by nginx)
Source (upload, then build on server):
- `tm_manager/frontend/src/**`        — all React/TS
- `tm_manager/frontend/public/**`     — favicon, logo
- `tm_manager/frontend/index.html`
- `tm_manager/frontend/package.json` + `package-lock.json`
- `tm_manager/frontend/vite.config.ts`
- `tm_manager/frontend/tsconfig*.json`
- `tm_manager/frontend/.oxlintrc.json`
Build OUTPUT (generated on server by `npm run build`, nginx serves this):
- `tm_manager/frontend/dist/`
Do NOT upload: `node_modules/`, `dist/`, `_run_fe.*`

### Shared
- `deploy/`  — this folder: services, nginx conf, env templates
- One Postgres database `datascraper` — both #1 and #2 use it
- One Python venv `.venv` at repo root — both #1 and #2 import from it
