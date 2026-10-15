"""
Application configuration — loaded from .env or environment variables.

Quick setup
-----------
1. Copy .env.example → .env
2. Fill in your email address and app password
3. Restart the app

Gmail users
-----------
• Enable 2-Step Verification: https://myaccount.google.com/security
• Create an App Password:     https://myaccount.google.com/apppasswords
• Select "Mail" → generate → paste the 16-char code as OTP_APP_PASSWORD

Outlook/Hotmail users
---------------------
• Enable 2-Step Verification on your Microsoft account
• Create an App Password in your account security settings
• Set OTP_IMAP_SERVER=outlook.office365.com

Yahoo users
-----------
• Go to Account Security → Generate app password
• Set OTP_IMAP_SERVER=imap.mail.yahoo.com

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


if os.environ.get('SCRAPER_LOAD_LEGACY_ENV', '1') == '1':
    _load_dotenv(_ENV_PATH)


def _fix_ca_bundle() -> None:
    """Some hosts (notably PostgreSQL 18's Windows installer) set a broken
    machine-wide CURL_CA_BUNDLE / SSL_CERT_FILE that points at a file that
    doesn't exist, which breaks every `requests` HTTPS call. If the current
    value is missing, repoint it at certifi's bundle. No-op when the env is
    already fine or certifi isn't installed."""
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


# ── OTP email settings ────────────────────────────────────────────────────────
OTP_EMAIL_ADDRESS: str = _str("OTP_EMAIL_ADDRESS")
OTP_APP_PASSWORD: str = _str("OTP_APP_PASSWORD")
OTP_IMAP_SERVER: str = _str("OTP_IMAP_SERVER", "imap.gmail.com")
OTP_IMAP_PORT: int = _int("OTP_IMAP_PORT", 993)
OTP_TIMEOUT_SECONDS: int = _int("OTP_TIMEOUT_SECONDS", 60)
OTP_POLL_INTERVAL: int = _int("OTP_POLL_INTERVAL", 3)
OTP_SENDER_FILTER: str = _str("OTP_SENDER_FILTER")   # e.g. "quickcompany.in"
OTP_SUBJECT_FILTER: str = _str("OTP_SUBJECT_FILTER")  # e.g. "OTP" or "verification"

# ── Grok API (CAPTCHA auto-solve fallback) ───────────────────────────────────
GROK_API_KEY: str = _str("GROK_API_KEY")

# ── PostgreSQL connection ─────────────────────────────────────────────────────
PG_HOST: str = _str("PG_HOST", "localhost")
PG_PORT: int = _int("PG_PORT", 5432)
PG_DBNAME: str = _str("PG_DBNAME", "datascraper")
PG_USER: str = _str("PG_USER", "postgres")
PG_PASSWORD: str = _str("PG_PASSWORD", "")   # empty = local trust auth, no password

# Optional client-cert auth (see pg_hba.conf's "cert" rule on Contabo) —
# lets this machine authenticate by identity instead of by IP, so a VPN or
# ISP change never needs a new firewall/pg_hba rule. When set, db.py sends
# these instead of (or alongside) PG_PASSWORD; when empty, every existing
# password-only install is unaffected. sslmode=require (not verify-full)
# since Contabo's server cert is still the default self-signed one — this
# only authenticates the CLIENT, it doesn't verify the server's identity.
PG_SSLCERT: str = _str("PG_SSLCERT", "")
PG_SSLKEY: str = _str("PG_SSLKEY", "")

# ── Google Drive document storage (see google_drive.py) ─────────────────────
# OAuth2 with a personal Google account, not a service account (a service
# account has no access to a personal account's own Drive storage quota) —
# see scripts/google_drive_auth_setup.py for the one-time consent flow that
# produces GOOGLE_DRIVE_REFRESH_TOKEN. Duplicated into tm_manager/.env too
# (that process reads its own separate .env, no shared-secrets mechanism
# exists between the two) since tm_manager's serving routes need Drive
# access just as much as this scraper's upload path does.
GOOGLE_DRIVE_CLIENT_ID: str = _str("GOOGLE_DRIVE_CLIENT_ID")
GOOGLE_DRIVE_CLIENT_SECRET: str = _str("GOOGLE_DRIVE_CLIENT_SECRET")
GOOGLE_DRIVE_REFRESH_TOKEN: str = _str("GOOGLE_DRIVE_REFRESH_TOKEN")
GOOGLE_DRIVE_ROOT_FOLDER_ID: str = _str("GOOGLE_DRIVE_ROOT_FOLDER_ID")   # blank = create automatically on first use

# ── DB connection pool ───────────────────────────────────────────────────────
DB_POOL_MIN: int = _int("DB_POOL_MIN", 2)
DB_POOL_MAX: int = _int("DB_POOL_MAX", 10)

# ── Warm session pool (fast-lane IP India logins kept alive) ─────────────────
WARM_POOL_ENABLED: bool = os.environ.get("WARM_POOL_ENABLED", "1") == "1"
WARM_POOL_MIN_READY: int = _int("WARM_POOL_MIN_READY", 1)          # alert if fewer lanes ready
WARM_POOL_MAX_SESSION_AGE_SECONDS: int = _int("WARM_POOL_MAX_SESSION_AGE_SECONDS", 1500)  # ~25 min
WARM_POOL_MAX_CONCURRENT: int = _int("WARM_POOL_MAX_CONCURRENT", 0)  # 0 = no cap (= #warm accts)
WARM_POOL_LIVENESS_MAX_AGE_SECONDS: int = _int("WARM_POOL_LIVENESS_MAX_AGE_SECONDS", 45)
# How many warm accounts (first N by id) the batch auto-fetch queue may
# NEVER claim — kept free for manual TM Search / single-lookup use so a big
# batch job can't tie up every warm lane and force a manual search to fall
# back to a slow cold login. 0 = no reservation (old behavior).
WARM_POOL_PRIORITY_COUNT: int = _int("WARM_POOL_PRIORITY_COUNT", 2)

# ── OTP read tuning ──────────────────────────────────────────────────────────
OTP_USE_IDLE: bool = os.environ.get("OTP_USE_IDLE", "1") == "1"

# ── Outbound proxy for IP India scraping (empty = direct) ───────────────────
SCRAPE_PROXY_URL: str = _str("SCRAPE_PROXY_URL")

# ── Staleness-based auto-refresh (tm_manager side reads these too) ───────────
REFRESH_INTERVAL_DAYS: int = _int("REFRESH_INTERVAL_DAYS", 7)
STALE_REFRESH_BATCH: int = _int("STALE_REFRESH_BATCH", 20)

# ── HTTP server ─────────────────────────────────────────────────────────────
USE_WAITRESS: bool = os.environ.get("USE_WAITRESS", "0") == "1"
BASIC_AUTH_USER: str = _str("BASIC_AUTH_USER")
BASIC_AUTH_PASS: str = _str("BASIC_AUTH_PASS")

# ── PDF Scraper (Scraper B) — standalone service that saves/parses PDF
# bytes this app already downloaded from IP India. See pdf_scraper/app.py.
# Can point at a REMOTE machine's pdf_scraper (e.g. Contabo's, reached via
# an nginx route) instead of this machine's own local one — e.g. a worker
# whose own Postgres access is unreliable while its VPN is on can still
# hand documents to Contabo, which always has a working DB + Drive
# connection. Auth for that remote call is PDF_SCRAPER_AUTH_USER/PASS
# below, deliberately separate from BASIC_AUTH_USER/PASS: that pair gates
# THIS app's own incoming API (see the before_request block further
# down) — reusing it here would risk pointing this outgoing call at the
# WRONG credentials whenever PDF_SCRAPER_BASE_URL is a different machine
# than the one BASIC_AUTH_USER/PASS was configured for.
PDF_SCRAPER_BASE_URL: str = _str("PDF_SCRAPER_BASE_URL", "http://127.0.0.1:5002")
PDF_SCRAPER_AUTH_USER: str = _str("PDF_SCRAPER_AUTH_USER", "")
PDF_SCRAPER_AUTH_PASS: str = _str("PDF_SCRAPER_AUTH_PASS", "")

# ── Fleet worker mode (Contabo-coordinator + local-worker-fleet) ───────────
# WORKER-side config only — governs whether THIS scraper instance (running
# on a local machine, not Contabo) scopes its own account claiming to a
# machine-assigned slice instead of the global cold pool. Off by default,
# so every existing single-machine install is completely unaffected; every
# new code path this flag guards is additive (see db.claim_free_pool_account's
# optional machine_id param, and tm_manager's scheduler.py FLEET_MODE_ENABLED
# branch). Has NOTHING to do with, and never touches, this server's own
# live/interactive searches (Word Mark + Class search, TM Search, Auto
# Discover) — those keep running exactly as today regardless of this flag.
FLEET_MODE_ENABLED: bool = os.environ.get("FLEET_MODE_ENABLED", "0") == "1"
# Stable slug identifying this worker machine to the coordinator — set once
# per machine, never derived from hostname (those collide/change). Required
# when FLEET_MODE_ENABLED is on; ignored otherwise.
FLEET_MACHINE_KEY: str = _str("FLEET_MACHINE_KEY", "")
# Contabo's own HTTPS base URL, e.g. "https://mytmtracker.com" — where this
# worker's tm_manager-side scheduler polls /api/fleet/* (see tm_manager's
# scheduler.py). Kept here alongside FLEET_MODE_ENABLED/FLEET_MACHINE_KEY
# since all three describe "how this physical machine identifies itself and
# where it reports" — one env-configured identity per machine, not spread
# across two files' worth of settings.
FLEET_COORDINATOR_URL: str = _str("FLEET_COORDINATOR_URL", "")
# Where THIS machine's own tm_manager process listens (same box, not
# Contabo's public URL) — used by _run_batch_job's mid-session refill call
# (/api/fleet/claim-more) so one login can keep going back-to-back on fresh
# work instead of logging out every fixed N marks.
TM_MANAGER_BASE_URL: str = _str("TM_MANAGER_BASE_URL", "http://127.0.0.1:5005")
# Credentials for that same claim-more call — deliberately separate from
# BASIC_AUTH_USER/PASS, which instead gates THIS scraper's own incoming API
# globally (see this app's before_request block) and must stay unset/unused
# by tm_manager's existing unauthenticated calls into /api/scrape-appno-batch.
# Must match tm_manager/.env's OPS_API_USER/OPS_API_PASS (accepted there via
# _check_ops_credentials).
TM_MANAGER_OPS_USER: str = _str("TM_MANAGER_OPS_USER", "")
TM_MANAGER_OPS_PASS: str = _str("TM_MANAGER_OPS_PASS", "")


def is_otp_configured() -> bool:
    """Return True if email credentials have been supplied."""
    return bool(OTP_EMAIL_ADDRESS and OTP_APP_PASSWORD)


def is_drive_configured() -> bool:
    """Return True if Google Drive credentials have been supplied. Every
    Drive-touching code path (the upload hook in pdf_processor.py, the
    serving routes' Drive-first fallback in tm_manager/app.py, the
    standalone cleanup script) checks this first and falls back to
    today's local-disk-only behavior when False — so an environment with
    no Drive credentials configured (e.g. a fresh dev checkout) keeps
    working exactly as before this feature existed."""
    return bool(GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET and GOOGLE_DRIVE_REFRESH_TOKEN)


def save_settings(new_values: dict[str, str]) -> None:
    """
    Merge *new_values* into the .env file and update the running process.

    Existing keys in .env that are NOT in *new_values* are preserved.
    Empty-string values in *new_values* are skipped (keeps old value).
    """
    # Read existing .env
    existing: dict[str, str] = {}
    if _ENV_PATH.exists():
        with _ENV_PATH.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip().strip('"').strip("'")

    # Apply non-empty updates
    for k, v in new_values.items():
        if v.strip():
            existing[k] = v.strip()

    # Write back
    with _ENV_PATH.open("w", encoding="utf-8") as fh:
        fh.write("# Datascraper — Email OTP Configuration\n")
        fh.write("# Auto-generated. Do NOT commit this file to version control.\n\n")
        for k, v in existing.items():
            fh.write(f"{k}={v}\n")

    # Reflect into the live process
    for k, v in new_values.items():
        if v.strip():
            os.environ[k] = v.strip()

    _reload()


def _reload() -> None:
    """Refresh module-level constants from the current environment."""
    global OTP_EMAIL_ADDRESS, OTP_APP_PASSWORD, OTP_IMAP_SERVER, OTP_IMAP_PORT
    global OTP_TIMEOUT_SECONDS, OTP_POLL_INTERVAL, OTP_SENDER_FILTER, OTP_SUBJECT_FILTER
    global GROK_API_KEY
    global PG_HOST, PG_PORT, PG_DBNAME, PG_USER, PG_PASSWORD, PG_SSLCERT, PG_SSLKEY
    global DB_POOL_MIN, DB_POOL_MAX
    global WARM_POOL_ENABLED, WARM_POOL_MIN_READY, WARM_POOL_MAX_SESSION_AGE_SECONDS
    global WARM_POOL_MAX_CONCURRENT, WARM_POOL_LIVENESS_MAX_AGE_SECONDS, WARM_POOL_PRIORITY_COUNT
    global OTP_USE_IDLE, SCRAPE_PROXY_URL, REFRESH_INTERVAL_DAYS, STALE_REFRESH_BATCH
    global USE_WAITRESS, BASIC_AUTH_USER, BASIC_AUTH_PASS, PDF_SCRAPER_BASE_URL
    global PDF_SCRAPER_AUTH_USER, PDF_SCRAPER_AUTH_PASS
    OTP_EMAIL_ADDRESS = _str("OTP_EMAIL_ADDRESS")
    OTP_APP_PASSWORD = _str("OTP_APP_PASSWORD")
    OTP_IMAP_SERVER = _str("OTP_IMAP_SERVER", "imap.gmail.com")
    OTP_IMAP_PORT = _int("OTP_IMAP_PORT", 993)
    OTP_TIMEOUT_SECONDS = _int("OTP_TIMEOUT_SECONDS", 60)
    OTP_POLL_INTERVAL = _int("OTP_POLL_INTERVAL", 3)
    OTP_SENDER_FILTER = _str("OTP_SENDER_FILTER")
    OTP_SUBJECT_FILTER = _str("OTP_SUBJECT_FILTER")
    GROK_API_KEY = _str("GROK_API_KEY")
    PG_HOST = _str("PG_HOST", "localhost")
    PG_PORT = _int("PG_PORT", 5432)
    PG_DBNAME = _str("PG_DBNAME", "datascraper")
    PG_USER = _str("PG_USER", "postgres")
    PG_PASSWORD = _str("PG_PASSWORD", "")
    PG_SSLCERT = _str("PG_SSLCERT", "")
    PG_SSLKEY = _str("PG_SSLKEY", "")
    DB_POOL_MIN = _int("DB_POOL_MIN", 2)
    DB_POOL_MAX = _int("DB_POOL_MAX", 10)
    WARM_POOL_ENABLED = os.environ.get("WARM_POOL_ENABLED", "1") == "1"
    WARM_POOL_MIN_READY = _int("WARM_POOL_MIN_READY", 1)
    WARM_POOL_MAX_SESSION_AGE_SECONDS = _int("WARM_POOL_MAX_SESSION_AGE_SECONDS", 1500)
    WARM_POOL_MAX_CONCURRENT = _int("WARM_POOL_MAX_CONCURRENT", 0)
    WARM_POOL_LIVENESS_MAX_AGE_SECONDS = _int("WARM_POOL_LIVENESS_MAX_AGE_SECONDS", 45)
    WARM_POOL_PRIORITY_COUNT = _int("WARM_POOL_PRIORITY_COUNT", 2)
    OTP_USE_IDLE = os.environ.get("OTP_USE_IDLE", "1") == "1"
    SCRAPE_PROXY_URL = _str("SCRAPE_PROXY_URL")
    REFRESH_INTERVAL_DAYS = _int("REFRESH_INTERVAL_DAYS", 7)
    STALE_REFRESH_BATCH = _int("STALE_REFRESH_BATCH", 20)
    USE_WAITRESS = os.environ.get("USE_WAITRESS", "0") == "1"
    BASIC_AUTH_USER = _str("BASIC_AUTH_USER")
    BASIC_AUTH_PASS = _str("BASIC_AUTH_PASS")
    PDF_SCRAPER_BASE_URL = _str("PDF_SCRAPER_BASE_URL", "http://127.0.0.1:5002")
    PDF_SCRAPER_AUTH_USER = _str("PDF_SCRAPER_AUTH_USER", "")
    PDF_SCRAPER_AUTH_PASS = _str("PDF_SCRAPER_AUTH_PASS", "")
