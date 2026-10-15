#!/usr/bin/env bash
# One-shot deploy / update for the TM Tracker VPS.
# Run as the `tmtracker` user from /opt/tmtracker after `git pull` (or first upload).
#
#   sudo -u tmtracker bash /opt/tmtracker/deploy/deploy.sh
#
# Then (as root):  systemctl restart tmtracker-scraper tmtracker-backend && systemctl reload nginx

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo ">> deploy root: $ROOT"

# 1. Python venv + deps (shared by scraper + backend)
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip install -q -r tm_manager/requirements.txt
echo ">> python deps installed"

# 2. env files present?
[ -f .env ] || { echo "!! missing /opt/tmtracker/.env — cp deploy/.env.production.example .env and fill it"; exit 1; }
[ -f tm_manager/.env ] || { echo "!! missing tm_manager/.env — cp deploy/tm_manager.env.production.example tm_manager/.env and fill it"; exit 1; }

# 3. DB schema (get_connection() creates tables on first call) + indexes
.venv/bin/python -c "import db; db.get_connection().close()"
( cd tm_manager && ../.venv/bin/python -c "import db, tm_leads_db; c=db.get_connection(); c.close()" )
.venv/bin/python scripts/apply_perf_indexes.py
echo ">> DB schema + indexes applied"

# 4. Frontend build
if ! command -v npm >/dev/null; then
  echo "!! npm not found — install Node 20+ (as root):"
  echo "   curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt install -y nodejs"
  exit 1
fi
( cd tm_manager/frontend && npm ci && npm run build )
echo ">> frontend built -> tm_manager/frontend/dist"

# 5. log dir
mkdir -p /var/log/tmtracker 2>/dev/null || sudo mkdir -p /var/log/tmtracker || true

echo
echo ">> done. Now (as root):"
echo "   systemctl restart tmtracker-scraper tmtracker-backend"
echo "   systemctl reload nginx"
echo "   curl -s http://127.0.0.1:5001/api/warm-pool/status"
