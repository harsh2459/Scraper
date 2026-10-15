# TM Tracker — Launch on your Contabo VPS

**Server:** `62.146.239.158` (root)  ·  **Result:** `http://62.146.239.158`

There is exactly **ONE script** to run. Everything else is upload + one command.

---

## PART 1 — Upload the code (from your Windows PC)

Your code is already at `/opt/tmtracker` on the server (you did this).
If you ever need to re-upload, easiest is **WinSCP** (https://winscp.net):

1. New Session → SFTP → Host `62.146.239.158`, Port `22`, User `root`, Password = *(Contabo root password)*
2. Left = your PC (`H:\Datascraper`), Right = server (`/opt/tmtracker`)
3. Drag these to the right, **skip the rest**:
   - files: `app.py config.py db.py email_otp.py ipindia_scraper.py warm_pool.py`
     `trademark_scraper_core.py folder_export.py ipindia_grouping.py requirements.txt`
   - folders: `tm_manager  scripts  deploy`
   - **DO NOT upload:** `node_modules dist .venv data __pycache__ *.db *.log .env .git .claude scratch video-software`

---

## PART 2 — Set your 6 values

On the server (SSH terminal), open the script's config block:

```bash
nano /opt/tmtracker/deploy/server-setup.sh
```

Near the top, the `CONFIG` block. Edit these (they already have working defaults —
change the passwords to something stronger when you can):

| Value | What it is |
|---|---|
| `SERVER_NAME` | `62.146.239.158` — or your domain if you have one |
| `DB_PASSWORD` | the Postgres password (already `Nvgalani@369`, matches the DB user you made) |
| `OTP_EMAIL` / `OTP_APP_PASSWORD` | a Gmail + its App Password — the scraper reads IP India OTP codes here |
| `LOGIN_USER` / `LOGIN_PASSWORD` | the website login popup (`admin` / `Nvgalani@369`) |

Save: **Ctrl+O**, Enter, **Ctrl+X**

---

## PART 3 — Run it

```bash
sudo bash /opt/tmtracker/deploy/server-setup.sh
```

Takes ~5–10 minutes. It installs packages, sets up Postgres, creates the Python
venv, builds all DB tables + the 22 performance indexes, compiles the frontend,
starts both background services, and configures nginx + the firewall.

**Two web apps, two subdomains** — both use the same `admin` / password login:

| URL | App | What it's for |
|---|---|---|
| `https://yourdomain.com`         | **TM Manager** | client portfolio, leads, invoices, status timeline, journal watch |
| `https://scraper.yourdomain.com` | **Scraper**    | run IP India scrapes, manage the OTP email pool (Settings), Auto Discover, scrape history |

### DNS — do this in GoDaddy BEFORE running the script (or before certbot)

GoDaddy → your domain → **DNS** → add these records (delete any "Parked" A record first):

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `@`       | `62.146.239.158` | 600 |
| A | `www`     | `62.146.239.158` | 600 |
| A | `scraper` | `62.146.239.158` | 600 |

Wait ~15 min, then check:  `nslookup yourdomain.com`  → should return `62.146.239.158`.

### In the script's CONFIG block, set:
```
SERVER_NAME="yourdomain.com"      # NOT the IP
```

> Contabo panel firewall (the yellow banner): leave it OFF, or if you turn it
> on, allow ports **22, 80, 443**.

Re-running the script is safe — it skips anything already done.

---

## PART 4 — Bring your existing data over (optional)

You have trademarks + leads in your local Postgres. To move them:

**On Windows:**
```
& "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" -h localhost -U postgres -d datascraper -Fc -f H:\Datascraper\dump.dump
```
(password: `12345678`)

**Upload `dump.dump`** to the server (WinSCP → `/opt/tmtracker/`), then **on the server:**
```bash
sudo -u tmtracker pg_restore --clean --if-exists --no-owner --role=tmtracker \
  -h localhost -U tmtracker -d datascraper /opt/tmtracker/dump.dump
sudo -u tmtracker /opt/tmtracker/.venv/bin/python /opt/tmtracker/scripts/apply_perf_indexes.py
sudo -u tmtracker bash -c 'cd /opt/tmtracker/tm_manager && ../.venv/bin/python scripts/backfill_status_history.py'
sudo systemctl restart tmtracker-scraper tmtracker-backend
rm /opt/tmtracker/dump.dump
```

Your restore also brings the 2 Gmail pool accounts, so fetching works right away.

---

## PART 5 — Make fetching fast (optional)

Add Gmail accounts to the "warm pool" — they stay logged in, so a fetch skips
the 60–90s OTP login and takes ~15s:

```bash
sudo -u tmtracker /opt/tmtracker/.venv/bin/python /opt/tmtracker/scripts/pool_accounts.py \
  add you@gmail.com "gmail app password" --role warm
# repeat for each account
sudo systemctl restart tmtracker-scraper
curl -s http://127.0.0.1:5001/api/warm-pool/status
```

---

## PART 6 — HTTPS for BOTH apps

1. DNS records for `@`, `www`, `scraper` all point at `62.146.239.158` (see the
   table near the top). Confirm: `nslookup scraper.yourdomain.com`.
2. `SERVER_NAME="yourdomain.com"` is set in the script (not the IP).
3. Run:
```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com -d scraper.yourdomain.com
```
Answer the prompts (email, agree, choose **2 = redirect HTTP→HTTPS**).

Result:
- `https://yourdomain.com` — TM Manager
- `https://scraper.yourdomain.com` — Scraper UI

Certificates auto-renew (certbot installs a systemd timer). Check anytime with
`sudo certbot renew --dry-run`.

---

## Everyday operations

| Task | Command |
|---|---|
| Restart everything | `sudo systemctl restart tmtracker-scraper tmtracker-backend && sudo systemctl reload nginx` |
| Watch scraper logs | `sudo journalctl -u tmtracker-scraper -f` |
| Watch backend logs | `sudo journalctl -u tmtracker-backend -f` |
| Check service status | `sudo systemctl status tmtracker-scraper tmtracker-backend` |
| Edit config later | `sudo nano /opt/tmtracker/.env` then restart |
| Deploy an update | re-upload changed files → `sudo -u tmtracker bash /opt/tmtracker/deploy/deploy.sh` → restart |
| Turn on auto-refresh worker | `sudo -u tmtracker sed -i 's/_AUTO_FETCH_ENABLED = False/_AUTO_FETCH_ENABLED = True/' /opt/tmtracker/tm_manager/scheduler.py && sudo systemctl restart tmtracker-backend` |

---

## What runs where

| Component | How it runs | Port | Public? |
|---|---|---|---|
| **Scraper** (IP India engine) | systemd `tmtracker-scraper` → `.venv/bin/python app.py` | 5001 | no (localhost) |
| **Backend** (TM Manager API) | systemd `tmtracker-backend` → `.venv/bin/python tm_manager/app.py` | 5005 | no (localhost) |
| **Frontend** (built React) | nginx serves `tm_manager/frontend/dist/` | 80 (443 w/ HTTPS) | **yes** |
| **Database** | PostgreSQL, DB `datascraper` | 5432 | no (localhost) |

nginx reverse-proxies `/api/*` → backend, `/scraper-api/*` → scraper.
Both apps also require the `admin` / password login on every request.

---

## If something's wrong

```bash
# service won't start — read the error:
sudo journalctl -u tmtracker-backend -n 50 --no-pager
sudo journalctl -u tmtracker-scraper -n 50 --no-pager

# nginx config error:
sudo nginx -t

# DB connection test:
sudo -u tmtracker /opt/tmtracker/.venv/bin/python -c "import sys; sys.path.insert(0,'/opt/tmtracker'); import db; db.get_connection().close(); print('DB OK')"

# is the frontend built?
ls -la /opt/tmtracker/tm_manager/frontend/dist/index.html
```
