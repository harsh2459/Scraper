"""
TM Manager configuration — loaded from .env or environment variables.

Shares the SAME Postgres database as the scraper app (h:/Datascraper) but is
its own separate config module by design — this app is a separate backend,
not a shared library, so it keeps its own copy of just the settings it needs.
"""

import os
from pathlib import Path

_ENV_PATH = Path(__file__).parent / ".env"


def _load_dotenv(path: Path) -> None:
    """Parse key=value pairs from *path* into os.environ (does not overwrite)."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


if os.environ.get('TM_LOAD_LEGACY_ENV', '1') == '1':
    _load_dotenv(_ENV_PATH)

FLEET_CONTROL_ENABLED = os.environ.get('TM_FLEET_CONTROL_ENABLED', '0') == '1'
TM_DATABASE_URL = os.environ.get('TM_DATABASE_URL', '')
AUTO_MIGRATE = os.environ.get('TM_AUTO_MIGRATE', '0' if FLEET_CONTROL_ENABLED else '1') == '1'
BACKGROUND_TASKS_ENABLED = os.environ.get('TM_BACKGROUND_TASKS_ENABLED', '1') == '1'
TM_BIND_HOST = os.environ.get('TM_BIND_HOST', '127.0.0.1' if FLEET_CONTROL_ENABLED else '0.0.0.0')
CONTROL_ARTIFACT_DIR = Path(os.environ.get('CONTROL_ARTIFACT_DIR', Path(__file__).parent.parent / '.runtime/control-artifacts')).resolve()
if os.environ.get('TM_ENV') == 'development':
    from urllib.parse import urlparse
    _database = urlparse(TM_DATABASE_URL)
    if _database.hostname not in ('localhost', '127.0.0.1', '::1') or not (_database.path.startswith('/fleet_dev') or _database.path.startswith('/fleet_test')):
        raise ValueError('TM development mode requires a loopback fleet_dev/fleet_test database.')


def _fix_ca_bundle() -> None:
    """Repoint a broken CURL_CA_BUNDLE / SSL_CERT_FILE at certifi's bundle
    (see the scraper's config.py for why). No-op when the env is fine."""
    try:
        import certifi
    except Exception:
        return
    good = certifi.where()
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        cur = os.environ.get(var)
        if not cur or not os.path.isfile(cur):
            os.environ[var] = good


_fix_ca_bundle()


def _str(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


# ── PostgreSQL connection — same database the scraper app uses ───────────────
PG_HOST: str = _str("PG_HOST", "localhost")
PG_PORT: int = _int("PG_PORT", 5432)
PG_DBNAME: str = _str("PG_DBNAME", "datascraper")
PG_USER: str = _str("PG_USER", "postgres")
PG_PASSWORD: str = _str("PG_PASSWORD", "")  # empty = local trust auth, no password

# Optional client-cert auth — see the root config.py's PG_SSLCERT/PG_SSLKEY
# comment (this machine's own copy of the same setting, since tm_manager
# reads its own separate .env).
PG_SSLCERT: str = _str("PG_SSLCERT", "")
PG_SSLKEY: str = _str("PG_SSLKEY", "")

# ── Scraper app's API — where auto-fetch requests are sent ───────────────────
SCRAPER_API_BASE_URL: str = _str("SCRAPER_API_BASE_URL", "http://127.0.0.1:5001")

# ── This app's own web server ─────────────────────────────────────────────────
TM_MANAGER_PORT: int = _int("TM_MANAGER_PORT", 5005)
USE_WAITRESS: bool = os.environ.get("USE_WAITRESS", "0") == "1"

# Ops/monitoring credential only (curl commands in deploy/HOW-TO-UPDATE.md) —
# NOT a real user login since real accounts live in the users table now.
# Falls back to the old BASIC_AUTH_USER/PASS names for a smooth transition
# (same .env values keep working under the new names).
OPS_API_USER: str = _str("OPS_API_USER") or _str("BASIC_AUTH_USER")
OPS_API_PASS: str = _str("OPS_API_PASS") or _str("BASIC_AUTH_PASS")
BASIC_AUTH_USER: str = _str("BASIC_AUTH_USER")
BASIC_AUTH_PASS: str = _str("BASIC_AUTH_PASS")

# ── DB connection pool ──────────────────────────────────────────────────────
DB_POOL_MIN: int = _int("DB_POOL_MIN", 2)
DB_POOL_MAX: int = _int("DB_POOL_MAX", 10)

# ── Staleness-based auto-refresh ───────────────────────────────────────────
REFRESH_INTERVAL_DAYS: int = _int("REFRESH_INTERVAL_DAYS", 1)
STALE_REFRESH_BATCH: int = _int("STALE_REFRESH_BATCH", 20)

# ── Outbound email (daily status-change digest) ─────────────────────────────
# A real Gmail account + app password, used purely for SENDING via SMTP —
# separate from the root scraper's own OTP_EMAIL_ADDRESS/OTP_APP_PASSWORD
# (those are for READING OTP codes from the IP India pool accounts, a
# different service entirely; the two apps never share credentials).
DIGEST_SMTP_HOST: str = _str("DIGEST_SMTP_HOST", "smtp.gmail.com")
DIGEST_SMTP_PORT: int = _int("DIGEST_SMTP_PORT", 465)
DIGEST_EMAIL_ADDRESS: str = _str("DIGEST_EMAIL_ADDRESS")
DIGEST_EMAIL_APP_PASSWORD: str = _str("DIGEST_EMAIL_APP_PASSWORD")
DIGEST_SEND_HOUR: int = _int("DIGEST_SEND_HOUR", 7)  # server local time, 24h


def is_digest_email_configured() -> bool:
    return bool(DIGEST_EMAIL_ADDRESS and DIGEST_EMAIL_APP_PASSWORD)


# ── Google Drive document storage (see H:\Datascraper\google_drive.py) ──────
# Same three secrets as the root scraper's own config.py, duplicated here —
# this app's serving routes (trademark_document/notice/logo) need Drive
# access to stream a document back just as much as the scraper's upload
# hook does, and there's no shared-secrets mechanism between the two
# separate .env files. See scripts/google_drive_auth_setup.py (root) for
# the one-time consent flow that produces GOOGLE_DRIVE_REFRESH_TOKEN.
GOOGLE_DRIVE_CLIENT_ID: str = _str("GOOGLE_DRIVE_CLIENT_ID")
GOOGLE_DRIVE_CLIENT_SECRET: str = _str("GOOGLE_DRIVE_CLIENT_SECRET")
GOOGLE_DRIVE_REFRESH_TOKEN: str = _str("GOOGLE_DRIVE_REFRESH_TOKEN")
GOOGLE_DRIVE_ROOT_FOLDER_ID: str = _str("GOOGLE_DRIVE_ROOT_FOLDER_ID")


def is_drive_configured() -> bool:
    return bool(GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET and GOOGLE_DRIVE_REFRESH_TOKEN)


# ── Fleet coordinator (Contabo-coordinator + local-worker-fleet) ────────────
# All of this is COORDINATOR-side config — it governs the daily planning
# tick and how "online" is judged for the fleet dashboard/planning. It has
# nothing to do with a worker machine's own scraper (see H:\Datascraper\
# config.py's FLEET_MODE_ENABLED/FLEET_MACHINE_KEY/FLEET_COORDINATOR_URL for
# that side). Only ever true/used on the Contabo process itself.
FLEET_COORDINATOR_ENABLED: bool = os.environ.get("FLEET_COORDINATOR_ENABLED", "0") == "1"
# How stale a machine's last heartbeat can be before it's treated as
# offline for planning/dashboard purposes — derived at read time, never
# stored (see fleet_machines' schema comment).
FLEET_HEARTBEAT_STALE_SECONDS: int = _int("FLEET_HEARTBEAT_STALE_SECONDS", 180)
# Rough daily fetch capacity per assigned email/OTP account, used to cap
# how much work a machine is handed so its share is actually finishable in
# a day rather than growing an unbounded backlog. Starting estimate from
# the scraper's existing 120s per-account cooldown plus login/search
# overhead — tune from real observed throughput once machines are live.
FLEET_PER_ACCOUNT_DAILY_CAP: int = _int("FLEET_PER_ACCOUNT_DAILY_CAP", 350)
# Server-local hour (24h) the daily planning tick runs at.
FLEET_PLAN_HOUR: int = _int("FLEET_PLAN_HOUR", 0)

# WORKER-side settings, read here too (same env var names as the scraper's
# own H:\Datascraper\config.py) so a single .env on a worker machine drives
# BOTH sibling processes (the scraper and this tm_manager instance)
# consistently — tm_manager's own scheduler.py needs FLEET_MODE_ENABLED to
# know whether to claim through fleet_assignments (claim_fleet_assigned_batch)
# instead of the global queue (claim_batch_pending_trademarks). False/empty
# by default on every install, including Contabo itself (which never sets
# these).
FLEET_MODE_ENABLED: bool = os.environ.get("FLEET_MODE_ENABLED", "0") == "1"
FLEET_MACHINE_KEY: str = _str("FLEET_MACHINE_KEY", "")
FLEET_COORDINATOR_URL: str = _str("FLEET_COORDINATOR_URL", "")
