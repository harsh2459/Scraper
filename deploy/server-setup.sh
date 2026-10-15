#!/usr/bin/env bash
###############################################################################
# TM Tracker — ONE-SHOT VPS setup.  Run this ONCE, as root, on a fresh
# Ubuntu 22.04/24.04 Contabo VPS.  It does EVERYTHING:
#
#   packages -> postgres -> app user -> python venv -> deps -> DB schema
#   -> perf indexes -> frontend build -> systemd services -> nginx -> firewall
#
# ---------------------------------------------------------------------------
# BEFORE YOU RUN IT
# ---------------------------------------------------------------------------
# 1. Upload the project to  /opt/tmtracker  (WinSCP, or scp a tar.gz).
#    The folder must already contain app.py, tm_manager/, deploy/, scripts/ ...
#
# 2. Edit the CONFIG block right below (DB password, Gmail, login password).
#
# 3. Run:
#      sudo bash /opt/tmtracker/deploy/server-setup.sh
#
# 4. When it finishes, open  http://<your-server-ip>  in a browser and log in
#    with the BASIC_AUTH user/password from the CONFIG block.
#
# Re-running is safe: it skips work already done.
###############################################################################

set -euo pipefail

# ===========================================================================
#  CONFIG  — edit these 6 values, then save.
# ===========================================================================
APP_DIR="/opt/tmtracker"            # where you uploaded the code
SERVER_NAME="62.146.239.158"        # your domain, or the VPS IP if no domain

DB_PASSWORD="Nvgalani@369"          # password for the 'tmtracker' postgres user

# The Gmail the scraper reads IP India OTP codes from (fallback mailbox — the
# warm/cold pool accounts are added later with scripts/pool_accounts.py).
OTP_EMAIL="harshbabariya2459@gmail.com"
OTP_APP_PASSWORD="zwam zhyk svyb wysm"

# The website login (browser popup). Pick something you'll remember.
LOGIN_USER="admin"
LOGIN_PASSWORD="Nvgalani@369"
# ===========================================================================


log()  { echo -e "\n\033[1;36m>>> $*\033[0m"; }
warn() { echo -e "\033[1;33m !! $*\033[0m"; }
die()  { echo -e "\033[1;31mXX $*\033[0m"; exit 1; }

[ "$(id -u)" = "0" ] || die "run this with sudo / as root"
[ -f "$APP_DIR/app.py" ] || die "no app.py in $APP_DIR — upload the project there first"
[ -f "$APP_DIR/tm_manager/app.py" ] || die "no tm_manager/app.py in $APP_DIR — upload is incomplete"


# ---------------------------------------------------------------------------
log "1/9  System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip postgresql nginx git curl ufw ca-certificates >/dev/null
if ! command -v node >/dev/null || [ "$(node -v 2>/dev/null | sed 's/v\([0-9]*\).*/\1/')" -lt 18 ]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
echo "python: $(python3 --version)   node: $(node -v)   psql: $(psql --version | awk '{print $3}')"


# ---------------------------------------------------------------------------
log "2/9  PostgreSQL database + user"
systemctl enable --now postgresql >/dev/null 2>&1 || true
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='datascraper'" | grep -q 1 \
  || sudo -u postgres psql -qc "CREATE DATABASE datascraper;"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='tmtracker'" | grep -q 1 \
  || sudo -u postgres psql -qc "CREATE USER tmtracker WITH PASSWORD '${DB_PASSWORD}';"
sudo -u postgres psql -qc "ALTER USER tmtracker WITH PASSWORD '${DB_PASSWORD}';"
sudo -u postgres psql -qc "GRANT ALL PRIVILEGES ON DATABASE datascraper TO tmtracker;"
sudo -u postgres psql -qc "ALTER DATABASE datascraper OWNER TO tmtracker;"
sudo -u postgres psql -d datascraper -qc "GRANT ALL ON SCHEMA public TO tmtracker;"
echo "db 'datascraper' owned by 'tmtracker'  OK"


# ---------------------------------------------------------------------------
log "3/9  App user + directories"
id tmtracker >/dev/null 2>&1 || useradd -r -m -d "$APP_DIR" -s /bin/bash tmtracker
mkdir -p "$APP_DIR" /var/log/tmtracker
chown -R tmtracker:tmtracker "$APP_DIR" /var/log/tmtracker
echo "user 'tmtracker', logs in /var/log/tmtracker  OK"


# ---------------------------------------------------------------------------
log "4/9  .env files"
SCRAPER_ENV="$APP_DIR/.env"
BACKEND_ENV="$APP_DIR/tm_manager/.env"

write_env_scraper() {
cat > "$SCRAPER_ENV" <<EOF
PG_HOST=localhost
PG_PORT=5432
PG_DBNAME=datascraper
PG_USER=tmtracker
PG_PASSWORD=${DB_PASSWORD}

OTP_EMAIL_ADDRESS=${OTP_EMAIL}
OTP_APP_PASSWORD=${OTP_APP_PASSWORD}
OTP_IMAP_SERVER=imap.gmail.com
OTP_IMAP_PORT=993
OTP_TIMEOUT_SECONDS=60
OTP_POLL_INTERVAL=3
OTP_SENDER_FILTER=nic.in
OTP_SUBJECT_FILTER=OTP
OTP_USE_IDLE=1

GROK_API_KEY=

DB_POOL_MIN=2
DB_POOL_MAX=12

WARM_POOL_ENABLED=1
WARM_POOL_MIN_READY=1
WARM_POOL_MAX_SESSION_AGE_SECONDS=1500
WARM_POOL_MAX_CONCURRENT=0
WARM_POOL_LIVENESS_MAX_AGE_SECONDS=45

SCRAPE_PROXY_URL=

USE_WAITRESS=1
BASIC_AUTH_USER=${LOGIN_USER}
BASIC_AUTH_PASS=${LOGIN_PASSWORD}
EOF
}

write_env_backend() {
cat > "$BACKEND_ENV" <<EOF
PG_HOST=localhost
PG_PORT=5432
PG_DBNAME=datascraper
PG_USER=tmtracker
PG_PASSWORD=${DB_PASSWORD}

SCRAPER_API_BASE_URL=http://127.0.0.1:5001
TM_MANAGER_PORT=5005

DB_POOL_MIN=2
DB_POOL_MAX=12

REFRESH_INTERVAL_DAYS=7
STALE_REFRESH_BATCH=20

USE_WAITRESS=1
BASIC_AUTH_USER=${LOGIN_USER}
BASIC_AUTH_PASS=${LOGIN_PASSWORD}
EOF
}

if [ -f "$SCRAPER_ENV" ]; then warn "keeping existing $SCRAPER_ENV"; else write_env_scraper; echo "wrote $SCRAPER_ENV"; fi
if [ -f "$BACKEND_ENV" ]; then warn "keeping existing $BACKEND_ENV"; else write_env_backend; echo "wrote $BACKEND_ENV"; fi
chown tmtracker:tmtracker "$SCRAPER_ENV" "$BACKEND_ENV"
chmod 600 "$SCRAPER_ENV" "$BACKEND_ENV"


# ---------------------------------------------------------------------------
log "5/9  Python venv + dependencies"
if [ ! -d "$APP_DIR/.venv" ]; then
  sudo -u tmtracker python3 -m venv "$APP_DIR/.venv"
fi
sudo -u tmtracker "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
sudo -u tmtracker "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
sudo -u tmtracker "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/tm_manager/requirements.txt"
echo "venv ready at $APP_DIR/.venv"


# ---------------------------------------------------------------------------
log "6/9  Database schema + performance indexes"
sudo -u tmtracker bash -c "cd '$APP_DIR'          && ./.venv/bin/python -c 'import db; db.get_connection().close()'"
sudo -u tmtracker bash -c "cd '$APP_DIR/tm_manager' && ../.venv/bin/python -c 'import db, tm_leads_db; c=db.get_connection(); tm_leads_db.get_connection().close(); c.close()'"
sudo -u tmtracker bash -c "cd '$APP_DIR'          && ./.venv/bin/python scripts/apply_perf_indexes.py"
echo "schema + indexes OK"


# ---------------------------------------------------------------------------
log "7/9  Frontend build"
sudo -u tmtracker bash -c "cd '$APP_DIR/tm_manager/frontend' && npm ci --no-audit --no-fund && npm run build"
[ -f "$APP_DIR/tm_manager/frontend/dist/index.html" ] || die "frontend build produced no dist/index.html"
echo "frontend built -> $APP_DIR/tm_manager/frontend/dist"


# ---------------------------------------------------------------------------
log "8/9  systemd services"
sed "s#/opt/tmtracker#${APP_DIR}#g" "$APP_DIR/deploy/tmtracker-scraper.service"  > /etc/systemd/system/tmtracker-scraper.service
sed "s#/opt/tmtracker#${APP_DIR}#g" "$APP_DIR/deploy/tmtracker-backend.service"  > /etc/systemd/system/tmtracker-backend.service
cp "$APP_DIR/deploy/tmtracker-backup.service" /etc/systemd/system/tmtracker-backup.service
sed "s#/opt/tmtracker#${APP_DIR}#g" -i /etc/systemd/system/tmtracker-backup.service
cp "$APP_DIR/deploy/tmtracker-backup.timer" /etc/systemd/system/tmtracker-backup.timer
chmod +x "$APP_DIR/deploy/backup.sh"
systemctl daemon-reload
systemctl enable --now tmtracker-scraper tmtracker-backend
systemctl enable --now tmtracker-backup.timer
sleep 4
systemctl is-active --quiet tmtracker-scraper  && echo "tmtracker-scraper  : active" || warn "tmtracker-scraper NOT active — journalctl -u tmtracker-scraper -n 40"
systemctl is-active --quiet tmtracker-backend  && echo "tmtracker-backend  : active" || warn "tmtracker-backend NOT active — journalctl -u tmtracker-backend -n 40"
systemctl is-active --quiet tmtracker-backup.timer && echo "tmtracker-backup.timer : active (daily 03:30)" || warn "tmtracker-backup.timer NOT active — systemctl status tmtracker-backup.timer"


# ---------------------------------------------------------------------------
log "9/9  nginx + firewall"
# TM Manager on  <SERVER_NAME>   ·   Scraper UI on  scraper.<SERVER_NAME>
sed "s/MAIN_DOMAIN/${SERVER_NAME}/g" "$APP_DIR/deploy/nginx-tmtracker.conf" > /etc/nginx/sites-available/tmtracker
ln -sf /etc/nginx/sites-available/tmtracker /etc/nginx/sites-enabled/tmtracker
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
yes | ufw enable >/dev/null 2>&1 || true
echo "nginx: main -> TM Manager, scraper.<domain> -> Scraper UI. firewall allows SSH + 80/443."


# ---------------------------------------------------------------------------
IS_IP=$(echo "$SERVER_NAME" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' && echo yes || echo no)
echo
echo -e "\033[1;32m============================================================\033[0m"
echo -e "\033[1;32m  DONE.\033[0m"
if [ "$IS_IP" = "yes" ]; then
  echo -e "\033[1;33m  (SERVER_NAME is an IP — the scraper subdomain won't work by IP.\033[0m"
  echo -e "\033[1;33m   Set SERVER_NAME to your domain and re-run to get both apps.)\033[0m"
  echo -e "\033[1;32m  TM Manager:  http://${SERVER_NAME}\033[0m"
else
  echo -e "\033[1;32m  TM Manager (portfolio, leads, invoices):\033[0m"
  echo -e "\033[1;32m      http://${SERVER_NAME}\033[0m"
  echo -e "\033[1;32m  Scraper UI (run scrapes, OTP pool, auto-discover, history):\033[0m"
  echo -e "\033[1;32m      http://scraper.${SERVER_NAME}\033[0m"
fi
echo -e "\033[1;32m  Login for both:  ${LOGIN_USER} / ${LOGIN_PASSWORD}\033[0m"
echo -e "\033[1;32m============================================================\033[0m"
if [ "$IS_IP" = "no" ]; then
  echo
  echo -e "\033[1;36m  Enable HTTPS on BOTH now (DNS for @, www, scraper must point here):\033[0m"
  echo "      sudo apt install -y certbot python3-certbot-nginx"
  echo "      sudo certbot --nginx -d ${SERVER_NAME} -d www.${SERVER_NAME} -d scraper.${SERVER_NAME}"
fi
echo
echo "Quick health checks:"
echo "  curl -s http://127.0.0.1:5001/api/warm-pool/status"
echo "  curl -s -u ${LOGIN_USER}:${LOGIN_PASSWORD} http://127.0.0.1:5005/api/dashboard | head -c 200"
echo
echo "NEXT (optional but recommended):"
echo "  * Restore your existing data:"
echo "      # on Windows:  pg_dump -h localhost -U postgres -d datascraper -Fc -f dump.dump"
echo "      # upload dump.dump to the server, then:"
echo "      sudo -u tmtracker pg_restore --clean --if-exists --no-owner --role=tmtracker -h localhost -U tmtracker -d datascraper dump.dump"
echo "      sudo -u tmtracker ${APP_DIR}/.venv/bin/python ${APP_DIR}/scripts/apply_perf_indexes.py"
echo "      sudo systemctl restart tmtracker-scraper tmtracker-backend"
echo
echo "  * Add warm-pool Gmail accounts (fast fetching):"
echo "      sudo -u tmtracker ${APP_DIR}/.venv/bin/python ${APP_DIR}/scripts/pool_accounts.py add you@gmail.com 'app pw' --role warm"
echo "      sudo systemctl restart tmtracker-scraper"
echo
echo "  * HTTPS for BOTH apps (needs DNS pointing here first):"
echo "      sudo apt install -y certbot python3-certbot-nginx"
echo "      sudo certbot --nginx -d ${SERVER_NAME} -d www.${SERVER_NAME} -d scraper.${SERVER_NAME}"
echo
echo "  * Enable the background auto-refresh worker (edit then restart):"
echo "      sudo -u tmtracker sed -i 's/_AUTO_FETCH_ENABLED = False/_AUTO_FETCH_ENABLED = True/' ${APP_DIR}/tm_manager/scheduler.py"
echo "      sudo systemctl restart tmtracker-backend"
