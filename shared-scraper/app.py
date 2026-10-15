"""
Local web UI for trademark scraping:
  • quickcompany.in  — public trademark registry
  • IP India e-Status — official government portal (with auto CAPTCHA + OTP)
sudo du -sh /opt/tmtracker/data /opt/tmtracker/.venv /var/lib/postgresql 2>/dev/null | sort -rh
Run with: python app.py
Then open: http://127.0.0.1:5000
"""

import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import sentry_sdk
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_cors import CORS
from sentry_sdk.integrations.flask import FlaskIntegration

import config
import db
import offline_queue
import local_store
import email_otp

# Sentry must be initialized before the Flask app is created. No-op (empty
# dsn) if SENTRY_DSN_SCRAPER isn't set — safe for local dev.
sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN_SCRAPER", ""),
    integrations=[FlaskIntegration()],
    traces_sample_rate=0.1,
    send_default_pii=False,
)
from email_otp import test_connection
from folder_export import DATA_DIR, slugify, write_trademark_search_folder
from ipindia_grouping import (
    APPLICANT_TYPES,
    CONTACT_VISIBILITY_LABELS,
    classify_applicant_type,
    classify_contact_visibility,
    group_searches_for_date,
)
from ipindia_scraper import (
    IPIndiaSession, SearchOutcome, SessionExpiredError, authenticate_pool_account,
    TmrPublicSearchSession, is_portal_blocked,
)
import warm_pool
import session_tracker
import trademark_scraper_core
from trademark_scraper_core import make_session, scrape_all_matches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ── Reply-to-examination-report status (Objected applications only) ──────────
REPLY_STATUS_LABELS = ["Replied", "Not Replied"]


def classify_reply_status(app_no: str) -> str:
    """
    Whether a reply to the exam report / objection has been filed for
    *app_no*, detected from the uploaded document filenames already saved
    to data/ipindia/<app_no>/ (e.g. "reply-to-exam-report-mis-r.pdf",
    "reply-to-notice-u-s-132-mis132.pdf") — no separate structured field
    exists for this on the portal. Applications with no such document saved
    (including ones whose documents haven't been downloaded yet) count as
    "Not Replied".
    """
    folder = DATA_DIR / "ipindia" / slugify(app_no)
    if not folder.is_dir():
        return "Not Replied"
    for pdf in folder.glob("*.pdf"):
        if "reply" in pdf.name.lower():
            return "Replied"
    return "Not Replied"

app = Flask(__name__)

# tm_manager's frontend (a different origin — localhost:5173/5005) calls the
# /api/scrape-appno + /api/scrape-status JSON endpoints directly from the
# browser (see api_scrape_appno's docstring) — without CORS those cross-origin
# fetches are blocked before this app's own error responses ever reach the
# caller. Scoped to /api/* only; the Jinja-rendered UI routes don't need it.
CORS(app, resources={r"/api/*": {"origins": "*"}})

# TODO(security): Add Flask-WTF CSRF protection if this app is exposed beyond
# localhost. For now it runs on 127.0.0.1 only (local use).

_session = make_session()

# ── Quickcompany jobs ────────────────────────────────────────────────────────
# job_id -> {"status": "running"|"done"|"error", "query": str, "done": int, "total": int, "message": str}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# ── IP India jobs ─────────────────────────────────────────────────────────────
# job_id -> {"status": "running"|"done"|"error", "step": str, "message": str, "results": list, "search_id": int|None}
_ipjobs: dict[str, dict] = {}
_ipjobs_lock = threading.Lock()

# Batch scrape jobs (see /api/scrape-appno-batch) — one job covers many app
# numbers processed with a single held session, mirroring Auto Discover's
# in-process loop instead of the one-HTTP-job-per-mark pattern _ipjobs uses.
# job_id -> {"status": "running"|"done", "total": int, "done": int,
#            "results": {app_no: {"outcome","search_id","message"}}, "started_at": float}
_batch_jobs: dict[str, dict] = {}
_batch_jobs_lock = threading.Lock()

# ── Fleet worker mode (Contabo-coordinator + local-worker-fleet) ───────────
# Resolved once per process (this scraper's own numeric fleet_machines.id,
# looked up directly against the same shared Postgres it already connects
# to — no HTTP round-trip needed for this). None whenever
# config.FLEET_MODE_ENABLED is off, which is every existing single-machine
# install — every caller below treats None as "don't scope by machine",
# preserving today's exact global behavior.
_fleet_machine_id: int | None = None


def _resolve_fleet_machine_id() -> int | None:
    """Looks up this worker's own fleet_machines.id by config.FLEET_MACHINE_KEY,
    caching the result for the life of the process. Returns None (and logs a
    warning) if fleet mode is on but the machine hasn't been registered yet
    via tm_manager's fleet dashboard/API — callers then fall back to
    unscoped (global) claim behavior rather than crashing, since a
    not-yet-registered worker is a normal transient state, not an error."""
    global _fleet_machine_id
    if not config.FLEET_MODE_ENABLED:
        return None
    if _fleet_machine_id is not None:
        return _fleet_machine_id
    if not config.FLEET_MACHINE_KEY:
        logging.warning("Fleet: FLEET_MODE_ENABLED is on but FLEET_MACHINE_KEY is not set.")
        return None
    conn = db.get_connection()
    try:
        try:
            row = conn.execute(
                "SELECT id FROM fleet_machines WHERE machine_key = %(k)s", {"k": config.FLEET_MACHINE_KEY},
            ).fetchone()
        except Exception:
            # fleet_machines lives in tm_manager's schema (see
            # otp_email_pool.assigned_machine_id's no-FK comment in db.py) —
            # if this worker's scraper starts before tm_manager has EVER run
            # once on this database, the table simply doesn't exist yet.
            # Treat that exactly like "not registered yet", not a crash.
            logging.warning(
                "Fleet: could not query fleet_machines (tm_manager may not have started on this "
                "database yet) — falling back to unscoped pool claiming for now.", exc_info=True,
            )
            return None
    finally:
        conn.close()
    if row is None:
        logging.warning(
            "Fleet: machine_key %r not found in fleet_machines yet — register it via the coordinator's "
            "dashboard/API first. Falling back to unscoped pool claiming for now.",
            config.FLEET_MACHINE_KEY,
        )
        return None
    _fleet_machine_id = row["id"]
    logging.info("Fleet: resolved this machine's id to %d (machine_key=%r).", _fleet_machine_id, config.FLEET_MACHINE_KEY)
    return _fleet_machine_id


def _fleet_today_progress() -> dict | None:
    """This machine's own today's fleet_assignments breakdown (pending/done/
    failed), for the /status page's overall progress line — same numbers
    the coordinator's Fleet dashboard shows for this machine, queried
    directly since both sides share one Postgres. None if fleet mode is off
    or this machine isn't registered yet (see _resolve_fleet_machine_id)."""
    machine_id = _resolve_fleet_machine_id()
    if machine_id is None:
        return None
    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                count(*) FILTER (WHERE status IN ('assigned', 'claimed')) AS pending,
                count(*) FILTER (WHERE status = 'done') AS done,
                count(*) FILTER (WHERE status = 'failed') AS failed
            FROM fleet_assignments
            WHERE machine_id = %(machine_id)s AND work_date = current_date
            """,
            {"machine_id": machine_id},
        ).fetchone()
    except Exception:
        logging.warning("Fleet: could not read today's fleet_assignments progress.", exc_info=True)
        return None
    finally:
        conn.close()
    if row is None:
        return None
    total = row["pending"] + row["done"] + row["failed"]
    return {"pending": row["pending"], "done": row["done"], "failed": row["failed"], "total": total}


# Pending IP India sessions waiting for user CAPTCHA confirmation
# pending_id -> IPIndiaSession (holds live HTTP session with cookies)
_pending_ip_sessions: dict[str, IPIndiaSession] = {}

# search_id -> (pending_id, view_token) — lets the result page offer a
# "Download Documents" button while the originating session is still alive.
# Transient only; not persisted (the token dies with the session).
_search_view_tokens: dict[int, tuple[str, str]] = {}

# ── Auto Discover jobs ────────────────────────────────────────────────────────
# job_id -> {
#   "status": "running"|"done"|"stopped"|"error"|"capped",
#   "phase": str, "message": str, "target_hits": int,
#   "hits": list[{"app_no": str, "search_id": int, "found_at": str}],
#   "attempts_count": int, "misses_count": int, "errors_count": int,
#   "email": str,
#   "mode": "single"|"series" (absent == "single"),
#   "current_series": str | None,   # series mode only
# }
_discover_jobs: dict[str, dict] = {}
_discover_lock = threading.Lock()
_discover_stop_events: dict[str, threading.Event] = {}

# Sessions mid-login for Auto Discover, waiting on a manually-entered OTP.
# pending_id -> {"session": IPIndiaSession, "email": str, "target_hits": int,
#                "delay_min": float, "delay_max": float, "mode": str}
_pending_discover: dict[str, dict] = {}

_DISCOVER_MIN = 1_000_000
_DISCOVER_MAX = 9_999_999
_MAX_CONSECUTIVE_ERRORS = 8

# ── Series tracker (exhaustive sequential coverage mode) ─────────────────────
# See db.py's "Series tracker" section for the persistence model. These
# constants govern the in-process scan loop only.
_SERIES_CHUNK_SIZE = 100                 # numbers per chunk (e.g. 7800001-7800100, 7800101-7800200, ...)
                                          # — also the checkpoint interval, so progress persists
                                          # after every completed chunk, not mid-chunk.
_SERIES_CHECKPOINT_EVERY_SECONDS = 30    # also checkpoint on a time basis, so a slow chunk (lots of
                                          # hits/retries) doesn't go too long between saves.
_SERIES_MAX_CONSECUTIVE_ERRORS = 8       # own constant — a full series legitimately needs up to
                                          # 100,000 attempts, so _MAX_TOTAL_ATTEMPTS (tuned for
                                          # random-mode "not converging on a hit target") doesn't apply here.

# ── Auto-launch (zero-click background scanning using the email pool) ────────
# No concurrency cap constant on purpose — capacity is simply "one job per
# currently-free, enabled pool account," recomputed every tick, so it scales
# automatically as accounts are added/removed in Settings.
_AUTO_LAUNCH_ENABLED = True              # kill switch — flip False to fall back to fully manual.
                                          # Re-enabled 2026-10-01: pool is now 17 enabled accounts
                                          # (was 2 when this was paused), so the background series
                                          # scanner no longer starves manual Auto Discover / live
                                          # lookups of the shared pool.
_AUTO_LAUNCH_POLL_SECONDS = 30           # idle-tick interval; also staggers launches one-per-tick
                                          # so a full pool doesn't all log in to the portal at once.
_AUTO_LAUNCH_RETRY_BACKOFF_SECONDS = 120 # wait this long after a failed launch attempt before retrying.
_AUTO_LAUNCH_PAUSED_POLL_SECONDS = 3     # re-check quickly while paused for an
                                          # active TM Search, so scanning
                                          # resumes promptly once it ends
                                          # rather than waiting a full idle tick.

# Count of TM Search (/api/scrape-appno) jobs currently in flight — while
# this is > 0, _auto_launch_loop holds off starting new series jobs so a
# user's live search doesn't queue up behind the scanner's own concurrent
# CAPTCHA/OTP logins on the shared pool. Doesn't touch jobs already
# running, only prevents starting new ones — so it never causes a hang,
# only slightly less background scanning while someone is actively
# searching. A plain int guarded by a lock is enough; this is a coarse
# pause signal; it doesn't need per-job identity.
_active_search_jobs = 0
_active_search_jobs_lock = threading.Lock()


def _search_jobs_active() -> bool:
    with _active_search_jobs_lock:
        return _active_search_jobs > 0

# ── Cluster expand jobs (QuickCompany name search → bulk IP India lookup) ─────
# job_id -> {
#   "status": "running"|"done"|"stopped"|"error",
#   "phase": str, "message": str, "mark_name": str,
#   "total_candidates": int, "skipped_existing": int,
#   "hits": list[{"app_no": str, "search_id": int, "found_at": str}],
#   "attempts_count": int, "misses_count": int, "errors_count": int,
#   "email": str,
# }
_cluster_jobs: dict[str, dict] = {}
_cluster_lock = threading.Lock()
_cluster_stop_events: dict[str, threading.Event] = {}

# Sessions mid-login for Cluster Expand, waiting on a manually-entered OTP.
# pending_id -> {"session": IPIndiaSession, "email": str, "mark_name": str}
_pending_cluster: dict[str, dict] = {}
_MAX_TOTAL_ATTEMPTS = 5000

# Known IP India trademark application statuses — shown in the history
# "By Status" tab even if no saved result currently has that status yet.
_KNOWN_IPINDIA_STATUSES = [
    "Formalities Chk Pass",
    "Accepted & Advertised",
    "Registered",
    "Objected",
    "Opposed",
    "Refused",
    "Abandoned",
    "Withdrawn",
    "Hearing Due",
    "Renewal Due",
    "Send To Vienna Codification",
]


# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def set_security_headers(response: Response) -> Response:
    """Apply a strict set of security headers to every response."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # CSP: allow scripts/styles only from self; no inline eval
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "   # inline scripts in templates
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "frame-ancestors 'none';"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


# ── Background job runner ─────────────────────────────────────────────────────

def _run_trademark_job(job_id: str, query: str) -> None:
    def on_progress(done, total, name):
        with _jobs_lock:
            _jobs[job_id].update(done=done, total=total, current=name)

    try:
        results = scrape_all_matches(_session, query, on_progress=on_progress)
    except requests.RequestException as exc:
        with _jobs_lock:
            _jobs[job_id].update(status="error", message=f"Request failed: {exc}")
        return

    conn = db.get_connection()
    for result in results:
        db.save_trademark_result(conn, result)
    conn.close()

    folder = write_trademark_search_folder(query, results)

    succeeded = [r for r in results if not r.get("error")]
    failed = [r for r in results if r.get("error")]

    if succeeded:
        message = f"Found {len(results)} match(es) for '{query}' — saved {len(succeeded)} to the database and to {folder}"
        if failed:
            message += f" ({len(failed)} failed)"
    else:
        message = f"No usable matches for '{query}': {failed[0]['error'] if failed else 'no results'}"

    with _jobs_lock:
        _jobs[job_id].update(status="done", message=message, done=len(results), total=len(results))


# ── Trademark scraper routes ──────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    conn = db.get_connection()
    tm_searches = db.list_trademark_searches(conn)
    conn.close()
    otp_configured = config.is_otp_configured()
    return render_template("index.html", tm_searches=tm_searches, message=None, error=None, otp_configured=otp_configured)


@app.route("/scrape", methods=["POST"])
def scrape():
    query = request.form.get("query", "").strip()
    if not query:
        conn = db.get_connection()
        tm_searches = db.list_trademark_searches(conn)
        conn.close()
        return render_template(
            "index.html",
            tm_searches=tm_searches,
            message=None,
            error="Enter a trademark/brand name first.",
            otp_configured=config.is_otp_configured(),
        )

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "query": query, "done": 0, "total": 0, "current": "", "message": ""}

    thread = threading.Thread(target=_run_trademark_job, args=(job_id, query), daemon=True)
    thread.start()

    conn = db.get_connection()
    tm_searches = db.list_trademark_searches(conn)
    conn.close()
    return render_template(
        "index.html",
        tm_searches=tm_searches,
        message=None,
        error=None,
        job_id=job_id,
        job_query=query,
        otp_configured=config.is_otp_configured(),
    )


@app.route("/scrape/status/<job_id>", methods=["GET"])
def scrape_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"status": "unknown"}), 404
    return jsonify(job)


@app.route("/search/<query>", methods=["GET"])
def trademark_search_detail(query):
    conn = db.get_connection()
    trademarks = db.trademarks_for_query(conn, query)
    conn.close()
    if not trademarks:
        abort(404)
    folder = DATA_DIR / slugify(query)
    return render_template("trademark_search_detail.html", query=query, trademarks=trademarks, folder=folder)


@app.route("/item/<int:trademark_id>", methods=["GET"])
def trademark_detail(trademark_id):
    conn = db.get_connection()
    trademark = db.get_trademark(conn, trademark_id)
    conn.close()
    if trademark is None:
        abort(404)
    return render_template("trademark_detail.html", t=trademark)


@app.route("/open-folder/<query>", methods=["POST"])
def open_folder(query):
    folder = DATA_DIR / slugify(query)
    _open_in_explorer(folder)
    return redirect(url_for("trademark_search_detail", query=query))


# ── Settings routes ───────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET"])
def settings():
    """Render the email OTP configuration page."""
    # Mask password for display — never send the real value to the frontend
    masked_pw = ""
    if config.OTP_APP_PASSWORD:
        masked_pw = "•" * min(len(config.OTP_APP_PASSWORD), 16)

    masked_grok_key = ""
    if config.GROK_API_KEY:
        masked_grok_key = "•" * min(len(config.GROK_API_KEY), 20)

    conn = db.get_connection()
    try:
        pool_accounts = db.list_pool_accounts(conn)
    finally:
        conn.close()

    return render_template(
        "settings.html",
        email_address=config.OTP_EMAIL_ADDRESS,
        masked_pw=masked_pw,
        imap_server=config.OTP_IMAP_SERVER,
        imap_port=config.OTP_IMAP_PORT,
        otp_timeout=config.OTP_TIMEOUT_SECONDS,
        sender_filter=config.OTP_SENDER_FILTER,
        subject_filter=config.OTP_SUBJECT_FILTER,
        masked_grok_key=masked_grok_key,
        pool_accounts=pool_accounts,
        pool_error=request.args.get("pool_error"),
        saved=request.args.get("saved") == "1",
        error=None,
    )


@app.route("/settings", methods=["POST"])
def settings_save():
    """Persist email OTP settings to .env and reload configuration."""
    email_addr = request.form.get("email_address", "").strip()
    app_pw = request.form.get("app_password", "").strip()
    imap_server = request.form.get("imap_server", "imap.gmail.com").strip()
    imap_port = request.form.get("imap_port", "993").strip()
    otp_timeout = request.form.get("otp_timeout", "60").strip()
    sender_filter = request.form.get("sender_filter", "").strip()
    subject_filter = request.form.get("subject_filter", "").strip()
    grok_api_key = request.form.get("grok_api_key", "").strip()

    # Validate inputs
    errors = []
    if email_addr and "@" not in email_addr:
        errors.append("Invalid email address.")
    if imap_port and not imap_port.isdigit():
        errors.append("IMAP port must be a number.")
    if otp_timeout and not otp_timeout.isdigit():
        errors.append("OTP timeout must be a number.")
    grok_key_is_new = grok_api_key and not all(c == "•" for c in grok_api_key)
    if grok_key_is_new and not grok_api_key.startswith("xai-"):
        errors.append('Grok API key looks incomplete — it should start with "xai-". Paste the full key.')

    if errors:
        masked_pw = "•" * min(len(config.OTP_APP_PASSWORD), 16) if config.OTP_APP_PASSWORD else ""
        masked_grok_key = "•" * min(len(config.GROK_API_KEY), 20) if config.GROK_API_KEY else ""
        return render_template(
            "settings.html",
            email_address=email_addr,
            masked_pw=masked_pw,
            imap_server=imap_server,
            imap_port=imap_port,
            otp_timeout=otp_timeout,
            sender_filter=sender_filter,
            subject_filter=subject_filter,
            masked_grok_key=masked_grok_key,
            saved=False,
            error=" ".join(errors),
        )

    new_values: dict[str, str] = {
        "OTP_EMAIL_ADDRESS": email_addr,
        "OTP_IMAP_SERVER": imap_server or "imap.gmail.com",
        "OTP_IMAP_PORT": imap_port or "993",
        "OTP_TIMEOUT_SECONDS": otp_timeout or "60",
        "OTP_SENDER_FILTER": sender_filter,
        "OTP_SUBJECT_FILTER": subject_filter,
    }
    # Only update password if user actually typed one (not the masked placeholder)
    if app_pw and not all(c == "•" for c in app_pw):
        new_values["OTP_APP_PASSWORD"] = app_pw
    # Only update Grok key if user actually typed one (not the masked placeholder)
    if grok_key_is_new:
        new_values["GROK_API_KEY"] = grok_api_key

    config.save_settings(new_values)
    return redirect(url_for("settings", saved="1"))


@app.route("/api/test-email", methods=["POST"])
def api_test_email():
    """
    Test IMAP credentials without saving them.
    Expects JSON: {"email": "...", "password": "...", "server": "...", "port": 993}

    If the password field contains only bullet characters (masked placeholder),
    we fall back to the currently saved config password.
    """
    data = request.get_json(silent=True) or {}
    email_addr = str(data.get("email", "")).strip()
    password   = str(data.get("password", "")).strip()
    server     = str(data.get("server", "imap.gmail.com")).strip()
    try:
        port = int(data.get("port", 993))
    except (TypeError, ValueError):
        port = 993

    # Detect masked placeholder (user didn't type a new password)
    is_masked = password and all(c == "•" for c in password)
    if is_masked:
        # Use the password already saved in config
        if config.OTP_APP_PASSWORD:
            password = config.OTP_APP_PASSWORD
        else:
            return jsonify({
                "ok": False,
                "message": "No saved password found. Please type your App Password in the field and try again."
            }), 400

    # Validate inputs
    if not email_addr or not password:
        return jsonify({"ok": False, "message": "Email and password are required."}), 400
    if "@" not in email_addr:
        return jsonify({"ok": False, "message": "Invalid email address."}), 400
    if port < 1 or port > 65535:
        return jsonify({"ok": False, "message": "Invalid port number."}), 400

    result = test_connection(email_addr, password, server, port)
    return jsonify(result)


# ── OTP email pool routes ─────────────────────────────────────────────────────
# Multiple IMAP-accessible inboxes (e.g. several Yahoo accounts) that Auto
# Discover can claim from automatically ("Auto (pool)" mode), instead of
# requiring one email typed in by hand each run. See db.py's "OTP email
# pool" section for the claim/release model.

@app.route("/settings/pool/add", methods=["POST"])
def settings_pool_add():
    email_addr = request.form.get("pool_email", "").strip()
    app_pw = request.form.get("pool_app_password", "").strip()
    imap_server = request.form.get("pool_imap_server", "imap.gmail.com").strip()
    imap_port_raw = request.form.get("pool_imap_port", "993").strip()

    errors = []
    if not email_addr or "@" not in email_addr:
        errors.append("Valid email address is required.")
    if not app_pw:
        errors.append("App password is required.")
    if not imap_port_raw.isdigit():
        errors.append("IMAP port must be a number.")

    if errors:
        return redirect(url_for("settings", pool_error=" ".join(errors)))

    conn = db.get_connection()
    try:
        db.add_pool_account(conn, email_addr, app_pw, imap_server or "imap.gmail.com", int(imap_port_raw))
    finally:
        conn.close()

    return redirect(url_for("settings", saved="1"))


@app.route("/settings/pool/<int:account_id>/remove", methods=["POST"])
def settings_pool_remove(account_id):
    conn = db.get_connection()
    try:
        row = db.get_pool_account(conn, account_id)
        if row and row.get("claimed_by_job_id"):
            return redirect(url_for("settings", pool_error="That account is currently in use by a running job — stop it first."))
        if row:
            db.remove_pool_account(conn, account_id)
    finally:
        conn.close()
    return redirect(url_for("settings", saved="1"))


@app.route("/settings/pool/<int:account_id>/toggle", methods=["POST"])
def settings_pool_toggle(account_id):
    conn = db.get_connection()
    try:
        row = db.get_pool_account(conn, account_id)
        if row:
            db.set_pool_account_enabled(conn, account_id, not row["enabled"])
    finally:
        conn.close()
    return redirect(url_for("settings", saved="1"))


@app.route("/settings/pool/<int:account_id>/role", methods=["POST"])
def settings_pool_role(account_id):
    """Assign this account to a dedicated fast lane — 'warm' (App-Number
    Search, kept permanently logged in via warm_pool.py), 'wordmark' (Word
    Mark + Class Search, reserved for _wm_get_or_login_session's cached
    session in app.py), or 'cold' (back to the regular shared pool)."""
    role = request.form.get("role", "cold").strip().lower()
    if role not in ("warm", "wordmark", "cold"):
        return redirect(url_for("settings", pool_error="Invalid role."))
    conn = db.get_connection()
    try:
        row = db.get_pool_account(conn, account_id)
        if row is None:
            return redirect(url_for("settings", pool_error="No such pool account."))
        updated = db.set_pool_account_role(conn, row["email_address"], role)
    finally:
        conn.close()
    try:
        if role == "warm":
            warm_pool.add_slot(updated)
        else:
            warm_pool.remove_slot(row["email_address"])
    except Exception:
        logging.exception("warm_pool live role-change failed for %s", row["email_address"])
    return redirect(url_for("settings", saved="1"))


@app.route("/api/test-pool-account/<int:account_id>", methods=["POST"])
def api_test_pool_account(account_id):
    """Test Connection for an already-saved pool row (unlike /api/test-email
    which tests ad hoc unsaved credentials) — also persists the result via
    db.record_pool_test_result so it's visible on next page load."""
    conn = db.get_connection()
    try:
        row = db.get_pool_account(conn, account_id)
        if not row:
            return jsonify({"ok": False, "message": "Account not found."}), 404
        result = test_connection(row["email_address"], row["app_password"], row["imap_server"], row["imap_port"])
        db.record_pool_test_result(conn, account_id, result["ok"], result["message"])
    finally:
        conn.close()
    return jsonify(result)


# ── IP India background job ───────────────────────────────────────────────────

def _should_skip_panels_fetch(conn, app_no: str, search_id: int, new_status: str | None) -> bool:
    """Status alone never proves that notices/documents are unchanged.

    Legacy callers fetch fully. The fleet worker owns incremental decisions
    using fresh lists, document metadata/checksums and periodic safety checks.
    """
    return False


def _fetch_all_tm_panels(
    sess: IPIndiaSession,
    app_no: str,
    view_token: str,
    request_verification_token: str | None,
    search_id: int | None = None,
    on_doc_saved=None,
    bundle: dict | None = None,
) -> dict:
    """
    Fetch every one of the portal's per-result panels — Uploaded Documents,
    Reminders, Correspondence & Notices, PR Details, the "e-main page"
    (ViewAppDetails), and the trademark image — CONCURRENTLY, all against
    this one session, instead of one-by-one in sequence. Each fetch is a
    single HTTP round-trip to a distinct endpoint (different RegisteredTM/*
    paths, plus /trademark/image), so there's no shared mutable state
    between them beyond the session's own cookies/tab-token, which requests
    already handles safely across threads for read-only calls like these.

    This function's ONLY job is to fetch and persist raw data as
    completely and quickly as possible — Documents still gets its PDFs
    downloaded and their TM-A/TM-1 fields extracted (that parsing already
    existed and stays, since it's what makes Application Date/Class etc.
    available at all), but PR Details and the e-main page are stored as
    completely raw, unparsed HTML (ipindia_page_fetches) — turning THAT
    into structured fields is a separate concern for a later pass to build
    on top of this data, not something to couple into the fetch itself.

    Returns a dict summarizing what was saved: {"documents": int,
    "reminders": int, "correspondence": int, "pr_details": bool,
    "app_details": bool, "image": bool}.

    bundle: pass this (from offline_queue.new_bundle) instead of a real
    search_id when the caller couldn't reach Contabo to create the
    ipindia_results row — every DB-bound save below goes into the bundle
    for later replay instead. search_id should be None whenever bundle is
    given (see _record_discover_hit, the only caller that uses this)."""
    import concurrent.futures

    results: dict = {
        "documents": 0, "reminders": 0, "correspondence": 0,
        "pr_details": False, "app_details": False, "image": False,
    }

    def _fetch_documents():
        return _save_uploaded_documents(
            sess, app_no, view_token, search_id=search_id, on_doc_saved=on_doc_saved,
            request_verification_token=request_verification_token, fetch_image=False, bundle=bundle,
        )

    def _fetch_reminders_correspondence():
        return _save_reminders_and_correspondence(
            sess, app_no, view_token, search_id=search_id,
            request_verification_token=request_verification_token, bundle=bundle,
        )

    def _fetch_pr_details():
        return sess.get_pr_details(view_token, request_verification_token)

    def _fetch_app_details():
        return sess.get_app_details(view_token, request_verification_token)

    def _fetch_image():
        return sess.get_trademark_image()

    with concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix=f"tmfetch-{slugify(app_no)}") as pool:
        futures = {
            pool.submit(_fetch_documents): "documents",
            pool.submit(_fetch_reminders_correspondence): "reminders_correspondence",
            pool.submit(_fetch_pr_details): "pr_details",
            pool.submit(_fetch_app_details): "app_details",
            pool.submit(_fetch_image): "image",
        }
        outcomes = {}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                outcomes[name] = fut.result()
            except Exception:
                logging.exception("Concurrent fetch of %s failed for %s", name, app_no)
                outcomes[name] = None

    results["documents"] = outcomes.get("documents") or 0
    rc = outcomes.get("reminders_correspondence")
    if isinstance(rc, tuple) and len(rc) == 2:
        results["reminders"], results["correspondence"] = rc

    pr_html = outcomes.get("pr_details")
    app_html = outcomes.get("app_details")
    if bundle is not None:
        # Offline mode (see offline_queue.py) — search_id is None here by
        # construction (the caller only passes a bundle when it couldn't
        # get a real search_id), so these go in the bundle for replay
        # instead of hitting the DB with a None id that'll just be
        # overwritten by the real one at replay time anyway.
        if pr_html:
            offline_queue.add_page_fetch(bundle, kind="pr_details", raw_html=pr_html)
            results["pr_details"] = True
        if app_html:
            offline_queue.add_page_fetch(bundle, kind="app_details", raw_html=app_html)
            results["app_details"] = True
    else:
        conn = db.get_connection()
        try:
            if pr_html:
                db.save_ipindia_page_fetch(conn, app_no, search_id, "pr_details", pr_html)
                results["pr_details"] = True
            if app_html:
                db.save_ipindia_page_fetch(conn, app_no, search_id, "app_details", app_html)
                results["app_details"] = True
        finally:
            conn.close()

    # _save_uploaded_documents is called above with fetch_image=False (this
    # function fetches the image itself, concurrently, instead) — so this is
    # the only place the image gets written to disk and recorded. Saved
    # independently of whether Documents found anything at all: this is
    # exactly the "most fetched TMs have no image" bug this whole function
    # exists to fix — an image-only ipindia_trademark_images row (root
    # db.py) never depends on any PDF successfully parsing.
    image_bytes = outcomes.get("image")
    if image_bytes:
        try:
            folder = DATA_DIR / "ipindia" / slugify(app_no)
            folder.mkdir(parents=True, exist_ok=True)
            image_path = folder / "trademark-image.png"
            with open(image_path, "wb") as f:
                f.write(image_bytes)
            # The image file itself is ALWAYS written to disk above
            # regardless of mode — only the ipindia_trademark_images DB
            # row is skipped in local mode (this table has no bundle/
            # replay path of its own since it's keyed on app_no only, not
            # search_id, so there's nothing to link it to a later-synced
            # search row; a re-fetch after VPN is off will record it
            # normally). Skipping this specific call is exactly what fixes
            # the stall from 2026-10-01 (every image-bearing hit used to
            # burn a 30s PoolTimeout here even after bundle-mode already
            # handled everything else for that hit).
            if not local_store.LOCAL_MODE:
                conn = db.get_connection()
                try:
                    db.set_trademark_image_path(conn, app_no, str(image_path))
                finally:
                    conn.close()
            results["image"] = True
        except Exception:
            logging.exception("Could not save trademark image for %s", app_no)

    return results


def _save_uploaded_documents(
    sess: IPIndiaSession,
    app_no: str,
    view_token: str,
    search_id: int | None = None,
    on_doc_saved=None,
    request_verification_token: str | None = None,
    fetch_image: bool = True,
    bundle: dict | None = None,
) -> int:
    """
    Fetch and save every uploaded document (PDF) for a trademark hit to
    data/ipindia/<app_no>/<description>.pdf. For TM-A application forms,
    also extracts structured field data to a sibling <description>.json
    sidecar file, crops the trademark logo/device image (if any) to a
    sibling <description>-logo.png, and inserts it all into the
    tma_documents table. Returns how many PDFs were saved.

    bundle: offline mode (see offline_queue.py) — when given, search_id is
    None (no real ipindia_results row exists yet) and each document's
    extracted fields are appended to bundle["documents"] (via
    _send_to_pdf_scraper's own pending_sync fallback still running
    underneath, unchanged — the PDF and its JSON sidecar always land on
    disk regardless; bundle only affects whether the eventual DB row
    links back to a real search_id immediately or after this hit's own
    replay). Passed straight through to _send_to_pdf_scraper so the
    bundle's "documents" list reflects what actually got extracted.

    on_doc_saved, if given, is called with {"filename": ..., "url": ...}
    right after each PDF is written to disk — lets a caller (see
    _run_ipindia_job) surface documents to the user as they arrive instead
    of only after the whole batch finishes, so someone watching a slow
    multi-document trademark sees download links appear one by one rather
    than staring at a single opaque "fetching documents…" wait.

    fetch_image=False skips this function's own get_trademark_image() call
    — pass this when a caller (see _fetch_all_tm_panels) already fetches
    the image itself, concurrently, as its own independent step; fetching
    it twice would be wasted work and risks two different bytes for the
    same trademark landing in two different places.

    Saving to disk and TM-A/TM-1 field extraction are delegated to the
    standalone PDF Scraper service — see _send_to_pdf_scraper.
    """
    # IP India's own backend can still be assembling the "Uploaded Documents"
    # list for a few seconds right after ViewRegistration renders — an
    # immediate GetDocumentsDetails call can come back short (missing e.g.
    # the original TM-A application PDF) or empty, even though the full list
    # shows up if you just wait and ask again. This is more likely to bite
    # exactly under server load (slower portal responses), which is when it
    # matters most. Retry with a short gap and keep whichever fetch found
    # the most documents — never settle for a short list just because the
    # first attempt technically returned something.
    # Fetch the portal's own rendered trademark image FIRST (unless the
    # caller is already handling it — see fetch_image above), before
    # anything else touches this session — it's keyed off server-side
    # "current application" state that ViewRegistration just set, so it has
    # to happen right after the search, ahead of the documents/retry loop
    # below (which can take several seconds and doesn't touch that state,
    # but there's no reason to risk it). Preferred over cropping the logo
    # out of the TM-A PDF: the PDF can be missing/incomplete even when the
    # portal's own image is available right here.
    direct_logo_bytes = sess.get_trademark_image() if fetch_image else None

    docs = sess.get_uploaded_documents(view_token, request_verification_token)
    for _attempt in range(2):
        time.sleep(3)
        retry_docs = sess.get_uploaded_documents(view_token, request_verification_token)
        if len(retry_docs) > len(docs):
            docs = retry_docs
        elif docs:
            break  # stable (or already had a full list) — stop early

    folder = DATA_DIR / "ipindia" / slugify(app_no)
    folder.mkdir(parents=True, exist_ok=True)

    # Written to disk now regardless of whether any document below actually
    # parses — but NOT saved to tma_documents by itself: that table's "one
    # row per document" shape means an image-only row would become the
    # latest-by-extracted_at row and blank out a real PDF's already-saved
    # Application Date/Class/etc. fields (get_trademark_data always reads
    # just the newest row). It's attached as the priority logo_path when the
    # real extracted-document row is written below instead.
    direct_logo_path: str | None = None
    if direct_logo_bytes:
        try:
            logo_file = folder / "trademark-image.png"
            with open(logo_file, "wb") as lf:
                lf.write(direct_logo_bytes)
            direct_logo_path = str(logo_file)
        except Exception:
            logging.exception("Could not save direct trademark image for %s", app_no)

    if not docs:
        return 0

    saved = 0
    for i, doc in enumerate(docs, start=1):
        description = doc.get("description") or f"document-{i}"

        # Skip re-downloading a document this app_no already has on file
        # from a previous fetch — without this, every re-fetch (the daily
        # staleness refresh, a manual "Fetch now") re-downloads every
        # unchanged document and saves it again under a new -1/-2 suffix
        # (see _send_to_pdf_scraper's collision handling), since document_no
        # is a portal-side id that only lives in-memory for one fetch and
        # was never persisted to compare against on a later run. The saved
        # filename (derived from `description`, same slugify the scraper
        # already uses) is the only stable signal available across runs.
        existing_path = folder / f"{slugify(description)}.pdf"
        if existing_path.exists():
            continue

        pdf_bytes = sess.download_document(doc["document_no"])
        if not pdf_bytes:
            continue

        filename, extracted = _send_to_pdf_scraper(
            pdf_bytes, app_no, description, kind="document", search_id=search_id,
            direct_logo_path=direct_logo_path, bundle=bundle,
        )
        if filename is None:
            continue
        saved += 1

        if on_doc_saved is not None:
            try:
                on_doc_saved({
                    "filename": filename,
                    "url": f"/ipindia/document/{slugify(app_no)}/{filename}",
                })
            except Exception:
                logging.exception("on_doc_saved callback failed for %s", filename)
    return saved


def _send_to_pdf_scraper(
    file_bytes: bytes,
    app_no: str,
    description: str,
    kind: str = "document",
    search_id: int | None = None,
    direct_logo_path: str | None = None,
    ext: str = "pdf",
    bundle: dict | None = None,
) -> tuple[str | None, dict | None]:
    """Hand a downloaded PDF/HTML file's bytes to the standalone PDF Scraper
    (Scraper B — see pdf_scraper/app.py) to be saved to disk and, for
    kind="document", parsed for TM-A/TM-1 fields. Returns (saved_filename,
    extracted_fields_or_None), or (None, None) if the file could not be
    saved at all.

    ext: real file extension without a leading dot — reminder/correspondence
    downloads can come back as plain HTML rather than a real PDF.

    Falls back to saving the file locally (the old, pre-split behavior) if
    Scraper B is unreachable or errors — so a temporary outage of the PDF
    service never loses a document, at the cost of briefly reintroducing
    the old inline save/parse path for just that one file.
    """
    import json as _json
    from ipindia_scraper import extract_document_data, extract_logo_image

    try:
        resp = requests.post(
            f"{config.PDF_SCRAPER_BASE_URL}/api/process-pdf",
            files={"file": (f"{slugify(description)}.{ext}", file_bytes, "application/octet-stream")},
            data={
                "app_no": app_no,
                "description": description,
                "kind": kind,
                "ext": ext,
                **({"search_id": str(search_id)} if search_id else {}),
            },
            auth=(
                (config.PDF_SCRAPER_AUTH_USER, config.PDF_SCRAPER_AUTH_PASS)
                if config.PDF_SCRAPER_AUTH_USER and config.PDF_SCRAPER_AUTH_PASS else None
            ),
            timeout=30,
        )
        if resp.status_code == 200:
            result = resp.json()
            saved_path = result.get("saved_path")
            if saved_path:
                return Path(saved_path).name, result.get("extracted")
        logging.warning(
            "pdf_scraper declined %s/%s (HTTP %s) — falling back to local save",
            app_no, description, resp.status_code,
        )
    except requests.RequestException as exc:
        logging.warning(
            "Could not reach pdf_scraper for %s/%s (%s) — falling back to local save",
            app_no, description, exc,
        )

    # Fallback: save and (for documents) parse locally, exactly as this
    # function did before the PDF Scraper split.
    folder = DATA_DIR / "ipindia" / slugify(app_no)
    if kind == "reminder":
        folder = folder / "reminders"
    elif kind == "correspondence":
        folder = folder / "correspondence"
    folder.mkdir(parents=True, exist_ok=True)
    name = slugify(description)
    path = folder / f"{name}.{ext}"
    i = 1
    while path.exists():
        i += 1
        path = folder / f"{name}-{i}.{ext}"
    try:
        with open(path, "wb") as f:
            f.write(file_bytes)
    except Exception as exc:
        logging.warning("Could not save document %s: %s", path, exc)
        return None, None

    if kind != "document":
        return path.name, None

    extracted = None
    try:
        extracted = extract_document_data(file_bytes)
        if extracted:
            json_path = path.with_suffix(".json")
            with open(json_path, "w", encoding="utf-8") as jf:
                _json.dump(extracted, jf, indent=2, ensure_ascii=False)

            logo_path_str = direct_logo_path
            if not logo_path_str:
                try:
                    logo_bytes = extract_logo_image(file_bytes)
                    if logo_bytes:
                        logo_path = path.with_name(f"{path.stem}-logo.png")
                        with open(logo_path, "wb") as lf:
                            lf.write(logo_bytes)
                        logo_path_str = str(logo_path)
                except Exception:
                    logging.exception("Could not extract logo image from document %s", path)

            if bundle is not None:
                # Offline mode (see offline_queue.py) — the caller already
                # knows there's no real search_id yet (that's WHY bundle
                # was passed), so don't even try the DB here: go straight
                # into the bundle for replay alongside the search row and
                # every other panel, all sharing the one real search_id
                # that'll exist once offline_queue replays this hit.
                offline_queue.add_document(
                    bundle, source_pdf_filename=path.name, source_pdf_path=str(path),
                    extracted=extracted, logo_path=logo_path_str,
                )
            else:
                try:
                    conn = db.get_connection()
                    try:
                        db.save_tma_document(
                            conn,
                            app_no=app_no,
                            search_id=search_id,
                            source_pdf_filename=path.name,
                            source_pdf_path=str(path),
                            extracted=extracted,
                            logo_path=logo_path_str,
                        )
                    finally:
                        conn.close()
                except Exception:
                    # The PDF (and its extracted-fields JSON just above) are
                    # already safely on disk — only the database ROW failed,
                    # almost certainly because Contabo is unreachable right now
                    # (e.g. this machine's VPN is on). Queue it for automatic
                    # retry instead of losing the extraction work outright; see
                    # pending_sync.py's module docstring for the full mechanism.
                    import pending_sync
                    pending_sync.mark_pending(path, app_no, search_id, extracted, logo_path_str)
    except Exception:
        logging.exception("Could not extract fields from document %s", path)
    return path.name, extracted


def _save_reminders_and_correspondence(
    sess: IPIndiaSession,
    app_no: str,
    view_token: str,
    search_id: int | None = None,
    request_verification_token: str | None = None,
    bundle: dict | None = None,
) -> tuple[int, int]:
    """
    Fetch Reminders and Correspondence & Notices for a trademark hit, save
    each row to the DB, and download the linked document PDF for each row
    to data/ipindia/<app_no>/reminders/ and .../correspondence/.
    Returns (reminders_saved, correspondence_saved).

    bundle: offline mode (see offline_queue.py) — when given, search_id is
    None and each row is appended to the bundle instead of written to the
    DB; the downloaded reminder/correspondence document files themselves
    are unaffected (they're saved to disk unconditionally either way, same
    as before — only the ipindia_reminders/ipindia_correspondence DB row
    is deferred).
    """
    folder = DATA_DIR / "ipindia" / slugify(app_no)

    reminders_saved = 0
    try:
        reminders = sess.get_reminders(view_token, request_verification_token)
    except Exception:
        logging.exception("Failed to fetch reminders for %s", app_no)
        reminders = []
    if reminders:
        rem_folder = folder / "reminders"
        rem_folder.mkdir(parents=True, exist_ok=True)
        conn = None if bundle is not None else db.get_connection()
        try:
            for i, row in enumerate(reminders, start=1):
                doc_path = None
                if row.get("corres_num"):
                    try:
                        downloaded = sess.download_reminder_document(row["corres_num"])
                        if downloaded:
                            content, ext = downloaded
                            filename, _ = _send_to_pdf_scraper(
                                content, app_no, row["corres_num"], kind="reminder", ext=ext,
                            )
                            doc_path = filename
                    except Exception:
                        logging.exception("Failed to download reminder document for %s row %s", app_no, i)
                if bundle is not None:
                    offline_queue.add_reminder(bundle, row=row, document_path=doc_path)
                else:
                    db.save_ipindia_reminder(conn, app_no, search_id, row, doc_path)
                reminders_saved += 1
        finally:
            if conn is not None:
                conn.close()

    correspondence_saved = 0
    try:
        correspondence = sess.get_correspondence(view_token, request_verification_token)
    except Exception:
        logging.exception("Failed to fetch correspondence for %s", app_no)
        correspondence = []
    if correspondence:
        corr_folder = folder / "correspondence"
        corr_folder.mkdir(parents=True, exist_ok=True)
        conn = None if bundle is not None else db.get_connection()
        try:
            for i, row in enumerate(correspondence, start=1):
                doc_path = None
                if row.get("view_url"):
                    try:
                        downloaded = sess.download_correspondence_document(row["view_url"])
                        if downloaded:
                            content, ext = downloaded
                            name = row.get("corres_no") or f"correspondence-{i}"
                            filename, _ = _send_to_pdf_scraper(
                                content, app_no, name, kind="correspondence", ext=ext,
                            )
                            doc_path = filename
                    except Exception:
                        logging.exception("Failed to download correspondence document for %s row %s", app_no, i)
                if bundle is not None:
                    offline_queue.add_correspondence(bundle, row=row, document_path=doc_path)
                else:
                    db.save_ipindia_correspondence(conn, app_no, search_id, row, doc_path)
                correspondence_saved += 1
        finally:
            if conn is not None:
                conn.close()

    return reminders_saved, correspondence_saved


def _run_ipindia_job(
    job_id: str,
    email: str,
    query: str,
    search_type: str,
    pending_session: "IPIndiaSession | None" = None,
    otp_code: str | None = None,
    search_captcha_answer: str | None = None,
    pending_id: str | None = None,
    pool_account_id: int | None = None,
    on_job_end=None,
) -> None:
    """
    Background thread: verify OTP (if provided) → scrape → save → update job
    state.

    pool_account_id, if set, is released back to the OTP email pool the
    moment THIS job's own run ends (success, error, or otherwise) —
    deliberately not held for a later "search another number" reuse of the
    same session, so a pool account isn't tied up longer than the single
    search that claimed it. "Search another" (ipindia_search_another) never
    touches the pool at all; it just keeps using the already-authenticated
    sess, which stays alive independently of the pool claim.

    on_job_end, if given, is called exactly once when this job's run ends
    (success or failure) with the exception that ended it, or None on a
    clean finish — used by the warm session pool (warm_pool.py) to detect a
    SessionExpiredError and retire+refill its slot, without this function
    needing to know anything about warm pools itself.
    """

    def status(msg: str) -> None:
        with _ipjobs_lock:
            _ipjobs[job_id]["step"] = msg

    def _release_pool() -> None:
        if pool_account_id is not None:
            conn = db.get_connection()
            try:
                db.release_pool_account(conn, pool_account_id)
            finally:
                conn.close()

    # When THIS job owns a cold-pool session (pool_account_id set, no
    # pending_id "search another" reuse), log it out cleanly on the way out
    # so IP India releases the server-side session immediately instead of
    # leaving it "unclean" — which would block the next login on that
    # account for ~90s ("previous logout was not clean").
    _sess_holder: list = [None]

    def _logout_owned_session() -> None:
        if pool_account_id is not None and not pending_id and _sess_holder[0] is not None:
            try:
                _sess_holder[0].logout()
            except Exception:
                try:
                    _sess_holder[0].stop_keepalive()
                except Exception:
                    pass

    _end_exc: Exception | None = None
    _extras_thread: threading.Thread | None = None
    try:
        sess = None
        if pending_session is not None:
            sess = pending_session
            _sess_holder[0] = sess
            if otp_code is not None and not hasattr(sess, "search_prepare_data"):
                # Verifying OTP only if it wasn't verified already
                status("Verifying OTP with IP India…")
                ok, err = sess.verify_otp_only(otp_code)
                if not ok:
                    with _ipjobs_lock:
                        _ipjobs[job_id].update(status="error", message=err)
                    return
        else:
            # Fully automatic flow (fallback)
            sess = IPIndiaSession(email)
            _sess_holder[0] = sess
            status("Fetching CAPTCHA from IP India portal…")
            ok, err = sess.authenticate(on_status=status)
            if not ok:
                with _ipjobs_lock:
                    _ipjobs[job_id].update(status="error", message=err)
                return

        status(f"Searching for '{query}' ({search_type})…")
        if search_type == "appno":
            # Auto-solve the search CAPTCHA (regex + Grok fallback) if it
            # wasn't already prepared/solved by the caller.
            if not hasattr(sess, "search_prepare_data"):
                status("Fetching & solving search CAPTCHA…")
                prepare_data = sess.prepare_search_appno()
                if not prepare_data or prepare_data.get("auto_answer") is None:
                    with _ipjobs_lock:
                        _ipjobs[job_id].update(status="error", message="Could not auto-solve search CAPTCHA.")
                    return
                sess.search_prepare_data = prepare_data
                search_captcha_answer = str(prepare_data["auto_answer"])

            p = sess.search_prepare_data
            result = sess.execute_search_appno(
                app_no=query,
                captcha_answer=search_captcha_answer,
                public_key_pem=p["public_key_pem"],
                anti_forgery_token=p["anti_forgery_token"]
            )
            logging.info("execute_search_appno(%r) result keys: %r", query, list(result.keys()) if result else None)
            results = [result] if result else []
        else:
            results = sess.search_by_name(query)

        # Keep the session (and its keepalive thread) alive so the user can
        # search another number without repeating login/OTP. It's dropped
        # from search_prepare_data so a fresh CAPTCHA is required next time.
        if hasattr(sess, "search_prepare_data"):
            del sess.search_prepare_data
        if pending_id:
            _pending_ip_sessions[pending_id] = sess

        # Strip the transient document-view token before persisting (it's
        # only valid for the life of this session) — keep it server-side,
        # keyed by search_id, for the "Download Documents" button.
        view_token = None
        rvt = None
        for r in results:
            token = r.pop("_view_token", None) if isinstance(r, dict) else None
            if token:
                view_token = token
            rvt_token = r.pop("_request_verification_token", None) if isinstance(r, dict) else None
            if rvt_token:
                rvt = rvt_token
            r.pop("_raw_html", None) if isinstance(r, dict) else None

        conn = db.get_connection()
        search_id = db.save_ipindia_search(
            conn, query, search_type, email, results,
            error="" if results else "No results found."
        )
        conn.close()

        # Report done RIGHT NOW — the trademark data itself is already
        # safely committed (search_id exists) — instead of making the user
        # wait through document/reminder saving too. Documents stream in
        # below via docs_status/documents, updated live on the same job
        # dict, so a slow multi-document trademark shows its data
        # immediately and download links appear one by one as they finish
        # rather than everything being gated behind one long opaque wait.
        msg = (
            f"Found {len(results)} result(s) for '{query}'."
            if results else
            f"No results found for '{query}'. The search API endpoint may need adjustment after first login."
        )
        with _ipjobs_lock:
            _ipjobs[job_id].update(
                status="done",
                message=msg,
                results=results,
                search_id=search_id,
                pending_id=pending_id if pending_id else None,
                email=email,
                docs_status="pending" if view_token else "none",
                documents=[],
            )

        if view_token:
            if pending_id:
                _search_view_tokens[search_id] = (pending_id, view_token)

            def _on_doc_saved(doc: dict) -> None:
                with _ipjobs_lock:
                    job = _ipjobs.get(job_id)
                    if job is not None:
                        job.setdefault("documents", []).append(doc)

            # Documents/reminders save in the background now — the job
            # already reported "done" above, so there's no timeout to race
            # against here; this thread just keeps running (daemon) and
            # updates docs_status/documents as it goes for the frontend to
            # poll, until it finishes or the process exits.
            def _save_extras() -> None:
                docs_saved = 0
                reminders_saved = 0
                correspondence_saved = 0
                try:
                    panel_results = _fetch_all_tm_panels(
                        sess, query, view_token, rvt, search_id=search_id, on_doc_saved=_on_doc_saved,
                    )
                    docs_saved = panel_results["documents"]
                    reminders_saved = panel_results["reminders"]
                    correspondence_saved = panel_results["correspondence"]
                except Exception:
                    logging.exception("Failed to auto-save TM panels for %s", query)
                with _ipjobs_lock:
                    job = _ipjobs.get(job_id)
                    if job is not None:
                        job["docs_status"] = "done"
                        job["reminders_saved"] = reminders_saved
                        job["correspondence_saved"] = correspondence_saved
                        if docs_saved or reminders_saved or correspondence_saved:
                            bits = []
                            if docs_saved:
                                bits.append(f"{docs_saved} document(s)")
                            if reminders_saved:
                                bits.append(f"{reminders_saved} reminder(s)")
                            if correspondence_saved:
                                bits.append(f"{correspondence_saved} correspondence item(s)")
                            job["docs_message"] = f"Saved {', '.join(bits)}."

            with _ipjobs_lock:
                job = _ipjobs.get(job_id)
                if job is not None:
                    job["docs_status"] = "saving"
            _extras_thread = threading.Thread(target=_save_extras, daemon=True)
            _extras_thread.start()

    except Exception as exc:
        _end_exc = exc
        logging.exception("IP India job %s failed", job_id)
        with _ipjobs_lock:
            _ipjobs[job_id].update(status="error", message=f"Unexpected error: {exc}")
    finally:
        # Reflect final state into the persistent scrape_jobs table (survives
        # a scraper restart, unlike the in-memory _ipjobs dict).
        try:
            with _ipjobs_lock:
                _snap = dict(_ipjobs.get(job_id, {}))
            db.set_scrape_job_from_ipjob(job_id, _snap)
        except Exception:
            logging.exception("scrape_jobs write-back failed for %s", job_id)

        def _cleanup_after_extras() -> None:
            # Documents now save in a background thread that outlives this
            # function's own try/except (see above — the job reports "done"
            # before doc-saving even starts). Releasing the pool account or
            # logging out the session here, before that thread finishes,
            # would yank the session out from under an in-flight PDF
            # download. So cleanup itself runs in ITS OWN thread that
            # first waits (bounded) for _extras_thread, then does exactly
            # what the old synchronous finally block did.
            if _extras_thread is not None:
                _extras_thread.join(timeout=120)
            # A cold-pool session belongs only to this automatic job.  It
            # must be logged out after both a successful and a failed run:
            # releasing the DB claim alone leaves IP India's server-side
            # session open, and its next login imposes the ~90-second
            # "previous logout was not clean" cooldown.  Manual
            # "search another" sessions have pending_id set and are
            # deliberately excluded by _logout_owned_session().
            _logout_owned_session()
            _release_pool()
            if on_job_end is not None:
                try:
                    on_job_end(_end_exc)
                except Exception:
                    logging.exception("on_job_end callback failed for job %s", job_id)

        if _extras_thread is not None:
            threading.Thread(target=_cleanup_after_extras, daemon=True).start()
        else:
            _cleanup_after_extras()


# ── Auto Discover background job ─────────────────────────────────────────────

def _record_discover_hit(job_id: str, email: str, app_no: int, result: dict, sess: IPIndiaSession) -> None:
    """Persist a hit immediately, auto-save its documents, append it to the
    job's live hit list, and spawn a background Cluster Expand for the hit's
    own trade mark name — so every random find snowballs outward via
    QuickCompany instead of staying a single isolated record.

    Local-first mode (see local_store.py): when local_store.LOCAL_MODE is
    on (VPN active for IP India access), this makes ZERO network calls to
    Contabo — not "try, then fall back on failure" (that pattern is what
    killed series job b34ed6c31fae459a9670dc792ed4d100 on 2026-10-01: each
    failed attempt still burned a 30s PoolTimeout first). Every panel
    fetch below runs against the already-authenticated IP India session
    exactly as normal (that part has nothing to do with Postgres); the
    search row and every panel save go straight into a bundle that's
    written to local SQLite (local_store.save_local_hit) and synced to
    Contabo later by offline_queue's background loop (see app.py startup
    and local_store.get_unsynced_hits/mark_hit_synced). The job's own live
    "hits" list below gets this app_no with search_id=None in that case —
    the fleet dashboard / Result page won't show it until it syncs, but
    nothing about the actual scrape is lost.

    When local mode is OFF (normal operation, no VPN), behavior is
    unchanged from before this existed: try Contabo directly, and only
    fall back to offline_queue's disk-marker-then-replay path on an actual
    failure (e.g. a transient network blip, not an expected-every-time
    VPN block)."""
    view_token = result.pop("_view_token", None)
    rvt = result.pop("_request_verification_token", None)
    mark_name = (result.get("Trade Mark") or "").strip()

    search_id: int | None = None
    bundle: dict | None = None
    if local_store.LOCAL_MODE:
        bundle = offline_queue.new_bundle(str(app_no), "appno_discover", email, [result], error="")
    else:
        try:
            conn = db.get_connection()
            try:
                search_id = db.save_ipindia_search(
                    conn, str(app_no), "appno_discover", email, [result], error=""
                )
            finally:
                conn.close()
        except Exception:
            logging.warning("Could not save search for %s to Contabo (likely unreachable) — "
                             "queuing this hit locally instead.", app_no)
            bundle = offline_queue.new_bundle(str(app_no), "appno_discover", email, [result], error="")

    if view_token:
        try:
            _fetch_all_tm_panels(sess, str(app_no), view_token, rvt, search_id=search_id, bundle=bundle)
        except Exception:
            logging.exception("Failed to auto-save TM panels for %s", app_no)

    if bundle is not None:
        if local_store.LOCAL_MODE:
            local_store.save_local_hit(bundle)
        else:
            offline_queue.queue_bundle(bundle)

    found_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _discover_lock:
        _discover_jobs[job_id]["hits"].append({
            "app_no": str(app_no),
            "search_id": search_id,
            "found_at": found_at,
        })

    if mark_name:
        _spawn_cluster_expand_unattended(mark_name)


def _run_discover_job(
    job_id: str,
    email: str,
    sess: IPIndiaSession,
    target_hits: int,
    delay_min: float,
    delay_max: float,
    range_start: int,
    range_end: int,
    pool_account_id: int | None = None,
) -> None:
    """
    Background thread: given an already-authenticated session (login/OTP is
    done synchronously before this thread starts — see /discover/verify_otp),
    continuously search random application numbers within
    [range_start, range_end]. Runs until target_hits is reached, the stop
    event is set, or a safety limit trips.

    pool_account_id, if set, is released back to the OTP email pool
    (db.release_pool_account) when this job ends for any reason.
    """
    def phase(msg: str) -> None:
        with _discover_lock:
            _discover_jobs[job_id]["phase"] = msg

    stop_event = _discover_stop_events[job_id]

    def finish(status: str, message: str) -> None:
        with _discover_lock:
            _discover_jobs[job_id].update(status=status, message=message)

    try:
        attempts = 0
        misses = 0
        errors = 0
        consecutive_errors = 0

        def do_search(app_no: int) -> "SearchOutcome":
            nonlocal attempts, misses, errors, consecutive_errors
            outcome, result = sess.search_appno_classified(str(app_no))
            attempts += 1
            if outcome == SearchOutcome.HIT:
                consecutive_errors = 0
                _record_discover_hit(job_id, email, app_no, result, sess)
            elif outcome == SearchOutcome.MISS:
                misses += 1
                consecutive_errors = 0
            else:  # ERROR
                errors += 1
                consecutive_errors += 1
            with _discover_lock:
                _discover_jobs[job_id].update(
                    attempts_count=attempts, misses_count=misses, errors_count=errors,
                )
            return outcome

        def hits_count() -> int:
            with _discover_lock:
                return len(_discover_jobs[job_id]["hits"])

        def should_stop() -> bool:
            return (
                stop_event.is_set()
                or hits_count() >= target_hits
                or attempts >= _MAX_TOTAL_ATTEMPTS
                or consecutive_errors >= _MAX_CONSECUTIVE_ERRORS
            )

        while not should_stop():
            candidate = random.randint(range_start, range_end)
            phase(f"Trying application number {candidate}…")
            do_search(candidate)
            time.sleep(random.uniform(delay_min, delay_max))

        # ── Determine final status ──
        if stop_event.is_set():
            finish("stopped", f"Stopped by user after {attempts} attempt(s), {hits_count()} hit(s) found.")
        elif hits_count() >= target_hits:
            finish("done", f"Reached target of {target_hits} hit(s).")
        elif attempts >= _MAX_TOTAL_ATTEMPTS:
            finish("capped", f"Stopped after reaching the safety cap of {_MAX_TOTAL_ATTEMPTS} attempts without hitting the target.")
        elif consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            finish("error", "Too many consecutive errors (session likely expired) — stop and start a new run.")
        else:
            finish("stopped", f"Stopped after {attempts} attempt(s), {hits_count()} hit(s) found.")

    except Exception as exc:
        logging.exception("Auto Discover job %s failed", job_id)
        finish("error", f"Unexpected error: {exc}")
    finally:
        sess.stop_keepalive()
        if pool_account_id is not None:
            _release_pool_account_any_mode(pool_account_id)


def _run_series_job(
    job_id: str,
    email: str,
    sess: IPIndiaSession,
    delay_min: float,
    delay_max: float,
    only_series: str | None = None,
    pool_account_id: int | None = None,
) -> None:
    """
    Background thread: exhaustive-coverage mode.

    If only_series is None (the normal "Auto Suggest / Start Next" path):
    repeatedly claims the next queued/resumable series from
    ipindia_series_progress (see db.py) and auto-advances into the next
    queued series when one completes — runs until the queue is empty.

    If only_series is set (the "Scan this series" button on a series'
    detail page): claims exactly that one series, scans it to completion or
    Stop, and then ends the job WITHOUT advancing to any other series —
    a targeted one-off, independent of queue ordering.

    Either way, scanning within a series is SEQUENTIAL from its last
    checkpoint through range_end (unlike _run_discover_job's random
    sampling — full coverage is the point here), checkpointed periodically
    so a stop/crash loses at most one checkpoint interval of work.

    pool_account_id, if set, is released back to the OTP email pool
    (db.release_pool_account) when this job ends for any reason.
    """
    stop_event = _discover_stop_events[job_id]

    # Job-level cumulative counters (across every series this job has
    # worked on so far) — feed the existing attempts/misses/errors fields
    # in the status JSON, same shape the frontend already polls.
    job_attempts = 0
    job_misses = 0
    job_errors = 0

    def phase(msg: str, series: str | None) -> None:
        with _discover_lock:
            _discover_jobs[job_id]["phase"] = msg
            _discover_jobs[job_id]["current_series"] = series

    def finish(status: str, message: str) -> None:
        with _discover_lock:
            _discover_jobs[job_id].update(status=status, message=message)

    try:
        while not stop_event.is_set():
            conn = db.get_connection()
            if only_series:
                series_row = db.claim_specific_series(conn, only_series, job_id, email=email)
            else:
                series_row = db.claim_next_series(conn, job_id, email=email)
            conn.close()

            if series_row is None:
                if only_series:
                    finish("error", f"Series {only_series} is already being scanned by another run.")
                else:
                    finish("done", "Series queue is empty — add more series to the queue to continue.")
                return

            series = series_row["series"]
            range_end = series_row["range_end"]
            candidate = (series_row["last_checked_number"] or series_row["range_start"] - 1) + 1

            # Numbers already known from OTHER scraping (earlier random-mode
            # Auto Discover, manual searches) that happen to sit ahead of
            # this series' own checkpoint — skip these during the sequential
            # sweep instead of wastefully re-searching the live portal for
            # something already in the database. Skipped entirely in local
            # mode (see local_store.py) — at worst this means re-scanning a
            # few numbers IP India already has local-only results for, a
            # wasted search or two, never a stall or data loss.
            if local_store.LOCAL_MODE:
                already_known = set()
            else:
                conn_known = db.get_connection()
                try:
                    already_known = db.ipindia_scraped_app_nos_in_range(conn_known, series_row["range_start"], range_end)
                finally:
                    conn_known.close()

            # Per-series-only counters, exactly what update_series_progress
            # persists as this series' absolute totals-so-far.
            series_hits = series_row["hits_count"]
            series_misses = series_row["misses_count"]
            series_errors = series_row["errors_count"]
            consecutive_errors = 0
            last_checkpoint_time = time.monotonic()
            search_count = 0

            def checkpoint(last_checked: int) -> None:
                if local_store.LOCAL_MODE:
                    # Zero-network checkpoint — see local_store.py. Worst
                    # case if the process dies before this ever syncs: the
                    # next run re-scans from the LAST SYNCED checkpoint,
                    # same cost as any other crash-before-checkpoint,
                    # never data loss (hits themselves are saved
                    # separately via save_local_hit, not tied to this).
                    local_store.update_series_progress_local(
                        series, last_checked, series_hits, series_misses, series_errors,
                    )
                    return
                conn2 = db.get_connection()
                try:
                    db.update_series_progress(
                        conn2, series,
                        last_checked_number=last_checked,
                        hits_count=series_hits,
                        misses_count=series_misses,
                        errors_count=series_errors,
                    )
                finally:
                    conn2.close()

            def do_search(app_no: int) -> "SearchOutcome":
                nonlocal job_attempts, job_misses, job_errors
                nonlocal series_hits, series_misses, series_errors, consecutive_errors
                outcome, result = sess.search_appno_classified(str(app_no))
                job_attempts += 1
                if outcome == SearchOutcome.HIT:
                    consecutive_errors = 0
                    series_hits += 1
                    _record_discover_hit(job_id, email, app_no, result, sess)
                elif outcome == SearchOutcome.MISS:
                    job_misses += 1
                    series_misses += 1
                    consecutive_errors = 0
                else:  # ERROR
                    job_errors += 1
                    series_errors += 1
                    consecutive_errors += 1
                with _discover_lock:
                    _discover_jobs[job_id].update(
                        attempts_count=job_attempts, misses_count=job_misses, errors_count=job_errors,
                    )
                return outcome

            def chunk_label(app_no: int) -> str:
                chunk_start = ((app_no - series_row["range_start"]) // _SERIES_CHUNK_SIZE) * _SERIES_CHUNK_SIZE + series_row["range_start"]
                chunk_end = min(chunk_start + _SERIES_CHUNK_SIZE - 1, range_end)
                return f"{chunk_start}-{chunk_end}"

            phase(f"Series {series}, chunk {chunk_label(candidate)} — trying {candidate}…", series)
            while candidate <= range_end and not stop_event.is_set():
                if candidate in already_known:
                    # Already have this number's data from other scraping
                    # (earlier random-mode Auto Discover, manual searches) —
                    # skip the live portal search entirely.
                    candidate += 1
                    if time.monotonic() - last_checkpoint_time > _SERIES_CHECKPOINT_EVERY_SECONDS:
                        checkpoint(candidate - 1)
                        last_checkpoint_time = time.monotonic()
                    continue

                phase(f"Series {series}, chunk {chunk_label(candidate)} — trying {candidate}…", series)
                do_search(candidate)
                search_count += 1

                now = time.monotonic()
                if (search_count % _SERIES_CHUNK_SIZE == 0
                        or now - last_checkpoint_time > _SERIES_CHECKPOINT_EVERY_SECONDS):
                    checkpoint(candidate)
                    last_checkpoint_time = now

                if consecutive_errors >= _SERIES_MAX_CONSECUTIVE_ERRORS:
                    break

                candidate += 1
                time.sleep(random.uniform(delay_min, delay_max))

            checkpoint(min(candidate, range_end))

            if stop_event.is_set():
                conn3 = db.get_connection()
                try:
                    db.release_series_claim(conn3, series)
                finally:
                    conn3.close()
                finish("stopped", f"Stopped by user mid-series {series} at application number {candidate}.")
                return

            if consecutive_errors >= _SERIES_MAX_CONSECUTIVE_ERRORS:
                conn3 = db.get_connection()
                try:
                    db.release_series_claim(conn3, series)
                finally:
                    conn3.close()
                finish(
                    "error",
                    f"Too many consecutive errors in series {series} (session likely expired) — "
                    "progress saved; click Auto Suggest / Start Next to resume.",
                )
                return

            # candidate > range_end: this series is fully scanned — mark it
            # done and loop back to claim the next queued series (unless
            # this job was targeting only this one series).
            conn3 = db.get_connection()
            try:
                db.complete_series(conn3, series)
            finally:
                conn3.close()

            if only_series:
                finish("done", f"Series {series} fully scanned.")
                return

    except Exception as exc:
        logging.exception("Series job %s failed", job_id)
        with _discover_lock:
            current = _discover_jobs.get(job_id, {}).get("current_series")
        if current:
            conn4 = db.get_connection()
            try:
                db.release_series_claim(conn4, current)
            finally:
                conn4.close()
        finish("error", f"Unexpected error: {exc}")
    finally:
        sess.stop_keepalive()
        if pool_account_id is not None:
            conn5 = db.get_connection()
            try:
                db.release_pool_account(conn5, pool_account_id)
            finally:
                conn5.close()


# ── Cluster Expand background job ────────────────────────────────────────────
# Snowball crawl: seed mark name → QuickCompany search → harvest Application
# IDs from every match → look each one up on IP India (one authenticated
# session for the whole run) → the Trade Mark name on each IP India hit
# becomes a new seed → repeat. Runs until manually stopped (or a hard
# safety ceiling trips, same crash-guard Auto Discover uses).

_CLUSTER_MAX_TOTAL_ATTEMPTS = 20000


def _record_cluster_hit(job_id: str, email: str, app_no: str, result: dict, sess: IPIndiaSession) -> str | None:
    """Persist an IP India hit, auto-save its documents, append it to the
    job's live hit list, and return the mark name (for the next hop) if any."""
    view_token = result.pop("_view_token", None)
    rvt = result.pop("_request_verification_token", None)

    conn = db.get_connection()
    search_id = db.save_ipindia_search(
        conn, app_no, "appno_discover", email, [result], error=""
    )
    conn.close()

    if view_token:
        try:
            _fetch_all_tm_panels(sess, app_no, view_token, rvt, search_id=search_id)
        except Exception:
            logging.exception("Failed to auto-save TM panels for %s", app_no)

    found_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _cluster_lock:
        _cluster_jobs[job_id]["hits"].append({
            "app_no": app_no,
            "search_id": search_id,
            "found_at": found_at,
        })

    return (result.get("Trade Mark") or "").strip() or None


def _run_cluster_job(
    job_id: str,
    email: str,
    sess: IPIndiaSession,
    seed_name: str,
    delay_min: float,
    delay_max: float,
    pool_account_id: int | None = None,
) -> None:
    """
    Background thread: BFS crawl. Each frontier name is searched on
    QuickCompany (find_all_matches + per-profile scrape) to harvest
    Application IDs; each new Application ID is looked up on IP India with
    the already-authenticated session; each IP India hit's own Trade Mark
    name is queued as a new frontier name. De-dupes both names and app
    numbers already seen this run, plus app numbers already scraped in past
    runs (via db.ipindia_already_scraped_app_nos).

    pool_account_id, if set, is released back to the OTP email pool
    (db.release_pool_account) when this job ends for any reason — held for
    the crawl's whole lifetime (unlike single search, which releases right
    away), since there's no "search another" equivalent here to worry about.
    """
    def phase(msg: str) -> None:
        with _cluster_lock:
            _cluster_jobs[job_id]["phase"] = msg

    def finish(status: str, message: str) -> None:
        with _cluster_lock:
            _cluster_jobs[job_id].update(status=status, message=message)

    stop_event = _cluster_stop_events[job_id]
    qc_session = make_session()

    try:
        attempts = 0
        misses = 0
        errors = 0
        consecutive_errors = 0
        skipped_existing = 0

        seen_names: set[str] = {seed_name.strip().lower()}
        seen_app_nos: set[str] = set()
        frontier: list[str] = [seed_name]

        def should_stop() -> bool:
            return stop_event.is_set() or attempts >= _CLUSTER_MAX_TOTAL_ATTEMPTS or consecutive_errors >= _MAX_CONSECUTIVE_ERRORS

        while frontier and not should_stop():
            name = frontier.pop(0)
            phase(f"Searching QuickCompany for '{name}'…")

            try:
                matches = trademark_scraper_core.find_all_matches(qc_session, name)
            except Exception as exc:
                logging.warning("QuickCompany search failed for %r: %s", name, exc)
                matches = []

            candidate_app_nos: list[str] = []
            for match_name, profile_url in matches:
                m = re.match(r"^(\d{5,8})", profile_url.rsplit("/", 1)[-1])
                if not m:
                    continue
                app_no = m.group(1)
                if app_no in seen_app_nos:
                    continue
                seen_app_nos.add(app_no)
                candidate_app_nos.append(app_no)

            if candidate_app_nos:
                conn = db.get_connection()
                already = db.ipindia_already_scraped_app_nos(conn, candidate_app_nos)
                conn.close()
                if already:
                    skipped_existing += len(already)
                    with _cluster_lock:
                        _cluster_jobs[job_id]["skipped_existing"] = skipped_existing
                    candidate_app_nos = [a for a in candidate_app_nos if a not in already]

            with _cluster_lock:
                _cluster_jobs[job_id]["total_candidates"] += len(candidate_app_nos)

            for app_no in candidate_app_nos:
                if should_stop():
                    break
                phase(f"Looking up application {app_no} on IP India…")
                outcome, result = sess.search_appno_classified(app_no)
                attempts += 1

                if outcome == SearchOutcome.HIT:
                    consecutive_errors = 0
                    next_name = _record_cluster_hit(job_id, email, app_no, result, sess)
                    if next_name and next_name.strip().lower() not in seen_names:
                        seen_names.add(next_name.strip().lower())
                        frontier.append(next_name)
                elif outcome == SearchOutcome.MISS:
                    misses += 1
                    consecutive_errors = 0
                else:
                    errors += 1
                    consecutive_errors += 1

                with _cluster_lock:
                    _cluster_jobs[job_id].update(
                        attempts_count=attempts, misses_count=misses, errors_count=errors,
                    )
                time.sleep(random.uniform(delay_min, delay_max))

        if stop_event.is_set():
            finish("stopped", f"Stopped by user after {attempts} attempt(s), {len(_cluster_jobs[job_id]['hits'])} record(s) saved.")
        elif not frontier:
            finish("done", f"Frontier exhausted — {attempts} attempt(s), {len(_cluster_jobs[job_id]['hits'])} record(s) saved.")
        elif attempts >= _CLUSTER_MAX_TOTAL_ATTEMPTS:
            finish("capped", f"Stopped after reaching the safety cap of {_CLUSTER_MAX_TOTAL_ATTEMPTS} attempts.")
        else:
            finish("error", "Too many consecutive errors (session likely expired) — stop and start a new run.")

    except Exception as exc:
        logging.exception("Cluster Expand job %s failed", job_id)
        finish("error", f"Unexpected error: {exc}")
    finally:
        sess.stop_keepalive()
        if pool_account_id is not None:
            _release_pool_account_any_mode(pool_account_id)


def _spawn_cluster_expand_unattended(mark_name: str) -> None:
    """
    Auto-triggered Cluster Expand: logs in fully automatically (CAPTCHA +
    IMAP-polled OTP, no user present to type a code — same mechanism
    _run_ipindia_job's fallback path and Auto Discover's login CAPTCHA step
    use) and launches a background cluster job. Used to expand every hit
    Auto Discover finds. Requires config.is_otp_configured(); silently
    no-ops otherwise since there's no one to configure it mid-run.

    `email` is the account Auto Discover's own caller session is logged in
    as — it must NOT be reused here. IP India allows only one active login
    per account, and Auto Discover's session is still live when a hit comes
    in, so logging into the same account again here collides with it
    ("already logged in on another tab") — this was firing on every single
    Auto Discover hit (seen live 2026-09-14) and, worse, would even fight
    the warm pool if `email` happened to be a warm-reserved account. Instead
    claim a separate free account from the pool (excludes warm-reserved +
    cooling-down accounts — see db.claim_free_pool_account) so this never
    contends with the caller's own session.
    """
    if not mark_name or not config.is_otp_configured():
        return

    def _login_and_run():
        conn = db.get_connection()
        try:
            job_id = uuid.uuid4().hex
            pool_account = db.claim_free_pool_account(conn, job_id, machine_id=_resolve_fleet_machine_id())
        finally:
            conn.close()
        if pool_account is None:
            logging.info("Auto-expand skipped for %r — no free pool account right now.", mark_name)
            return

        account_email = pool_account["email_address"]
        pool_account_id = pool_account["id"]
        sess = IPIndiaSession(account_email)
        try:
            ok, err = sess.authenticate()
        except Exception:
            logging.exception("Auto-expand login crashed for %r (%s)", mark_name, account_email)
            conn = db.get_connection()
            try:
                db.release_pool_account(conn, pool_account_id)
            finally:
                conn.close()
            return
        if not ok:
            logging.warning("Auto-expand login failed for %r (%s): %s", mark_name, account_email, err)
            conn = db.get_connection()
            try:
                db.release_pool_account(conn, pool_account_id)
            finally:
                conn.close()
            return

        with _cluster_lock:
            _cluster_jobs[job_id] = {
                "status": "running",
                "phase": "Starting…",
                "message": "",
                "mark_name": mark_name,
                "total_candidates": 0,
                "skipped_existing": 0,
                "hits": [],
                "attempts_count": 0,
                "misses_count": 0,
                "errors_count": 0,
                "email": account_email,
                "auto_triggered": True,
            }
        _cluster_stop_events[job_id] = threading.Event()
        _run_cluster_job(job_id, account_email, sess, mark_name, 2.0, 5.0, pool_account_id=pool_account_id)

    threading.Thread(target=_login_and_run, daemon=True).start()


# ── IP India routes ───────────────────────────────────────────────────────────

@app.route("/ipindia", methods=["GET"])
def ipindia_index():
    return render_template(
        "ipindia.html",
        otp_configured=config.is_otp_configured(),
        step=1,
        error=None,
        **_pool_counts(),
    )



@app.route("/ipindia/captcha", methods=["POST"])
def ipindia_get_captcha():
    """
    Step 1 submit. Two paths:

    auth_mode="pool" (default when a free pool account exists): claim +
    fully auto-authenticate (CAPTCHA solve, OTP send, IMAP auto-read,
    verify — no human involved), then launch the search job directly, no
    OTP-entry screen at all — mirrors discover_start's pool branch.

    auth_mode="manual" (or no free pool account): original flow — fetch +
    auto-solve the login CAPTCHA, send the OTP request, and render step 3
    (OTP entry) for the human to type the code they receive. The CAPTCHA
    itself is never shown to the user either way.

    Accepts form fields: auth_mode, email, query, search_type
    """
    auth_mode   = request.form.get("auth_mode", "pool").strip()
    email       = request.form.get("email", "").strip()
    query       = request.form.get("query", "").strip()
    search_type = request.form.get("search_type", "name").strip()

    if not query:
        return render_template("ipindia.html",
                               otp_configured=config.is_otp_configured(),
                               error="Search query cannot be empty.", step=1,
                               prev_email=email, prev_query=query, prev_search_type=search_type,
                               **_pool_counts())

    if auth_mode == "pool":
        job_id_for_claim = uuid.uuid4().hex
        sess, pool_account, err = _pool_claim_and_authenticate(job_id_for_claim)
        if err:
            return render_template("ipindia.html",
                                   otp_configured=config.is_otp_configured(),
                                   error=err, step=1,
                                   prev_email=email, prev_query=query, prev_search_type=search_type,
                                   **_pool_counts())

        job_id, launch_err = _launch_ipindia_job(
            pool_account["email_address"], query, search_type, sess,
            otp_code=None, pending_id=None, pool_account_id=pool_account["id"],
        )
        if launch_err:
            conn = db.get_connection()
            try:
                db.release_pool_account(conn, pool_account["id"])
            finally:
                conn.close()
            return render_template("ipindia.html",
                                   otp_configured=config.is_otp_configured(),
                                   error=launch_err, step=1,
                                   prev_email=email, prev_query=query, prev_search_type=search_type,
                                   **_pool_counts())

        return render_template("ipindia.html",
                               otp_configured=config.is_otp_configured(),
                               error=None, message=None,
                               job_id=job_id, job_query=query)

    # Manual auth mode — original flow.
    if not email or "@" not in email:
        return render_template("ipindia.html",
                               otp_configured=config.is_otp_configured(),
                               error="Valid email address is required.", step=1,
                               prev_email=email, prev_query=query, prev_search_type=search_type,
                               **_pool_counts())

    if not config.is_otp_configured():
        return render_template("ipindia.html",
                               otp_configured=False,
                               error="Email OTP credentials not configured — go to Settings first.", step=1,
                               prev_email=email, prev_query=query, prev_search_type=search_type,
                               **_pool_counts())

    # Fetch + auto-solve CAPTCHA, then send OTP — all in one go.
    sess = IPIndiaSession(email)
    try:
        captcha_info = sess.get_captcha_for_display()
    except Exception as exc:
        logging.exception("CAPTCHA fetch failed")
        return render_template("ipindia.html",
                               otp_configured=config.is_otp_configured(),
                               error=f"Could not fetch CAPTCHA from IP India: {exc}", step=1,
                               prev_email=email, prev_query=query, prev_search_type=search_type,
                               **_pool_counts())

    auto_answer = captcha_info.get("auto_answer")
    if auto_answer is None:
        return render_template("ipindia.html",
                               otp_configured=config.is_otp_configured(),
                               error="Could not auto-solve login CAPTCHA (regex + Grok both failed). Check GROK_API_KEY in Settings.", step=1,
                               prev_email=email, prev_query=query, prev_search_type=search_type,
                               **_pool_counts())

    ok, err = sess.send_otp_only(auto_answer)
    if not ok:
        return render_template("ipindia.html",
                               otp_configured=config.is_otp_configured(),
                               error=f"Could not send OTP: {err}", step=1,
                               prev_email=email, prev_query=query, prev_search_type=search_type,
                               **_pool_counts())

    pending_id = uuid.uuid4().hex
    _pending_ip_sessions[pending_id] = sess

    # Render step 3 — OTP entry (CAPTCHA is never shown)
    return render_template("ipindia.html",
                           otp_configured=config.is_otp_configured(),
                           step=3,
                           pending_id=pending_id,
                           email=email,
                           query=query,
                           search_type=search_type)



def _launch_ipindia_job(
    email: str,
    query: str,
    search_type: str,
    sess: "IPIndiaSession | None",
    otp_code: str | None,
    pending_id: str | None,
    pool_account_id: int | None = None,
    existing_job_id: str | None = None,
    on_job_end=None,
) -> tuple[str, str | None]:
    """
    Shared job-launch logic for a single search, used by both the manual
    OTP-entry path (ipindia_scrape) and the pool auto-login path
    (ipindia_get_captcha's pool branch) — everything past "we have an
    authenticated session" is identical either way. If search_type=="appno"
    AND sess is already authenticated (not None — the fully-automatic
    Settings-mailbox fallback passes sess=None and lets _run_ipindia_job
    authenticate for itself inside the thread), solves the (separate,
    second) search CAPTCHA synchronously here so the background job doesn't
    need to — mirrors ipindia_scrape's existing behavior exactly. Returns
    (job_id, error_message); error_message is set (and no job started) only
    if the appno search-CAPTCHA step fails.

    existing_job_id: reuse an _ipjobs entry a caller already created (e.g.
    /api/scrape-appno's retry loop, so pollers see one continuous job across
    retries) instead of minting a fresh one.

    on_job_end: passed straight through to _run_ipindia_job — see its own
    docstring (used by warm_pool.py to detect SessionExpiredError).
    """
    job_otp_code = otp_code
    auto_captcha_answer = None
    if sess is not None and search_type == "appno":
        prepare_data = sess.prepare_search_appno()
        if not prepare_data:
            return "", "Failed to fetch search CAPTCHA from portal."
        if prepare_data.get("auto_answer") is None:
            return "", "Could not auto-solve search CAPTCHA (regex + Grok both failed). Check GROK_API_KEY in Settings."
        sess.search_prepare_data = prepare_data
        auto_captcha_answer = str(prepare_data["auto_answer"])
        job_otp_code = None  # OTP already verified by the caller — don't re-verify in the job thread

    job_id = existing_job_id or uuid.uuid4().hex
    with _ipjobs_lock:
        _ipjobs[job_id] = {
            "status": "running",
            "step": "Starting…",
            "message": "",
            "results": [],
            "search_id": None,
            "query": query,
            "search_type": search_type,
        }

    thread = threading.Thread(
        target=_run_ipindia_job,
        args=(job_id, email, query, search_type, sess, job_otp_code, auto_captcha_answer, pending_id),
        kwargs={"pool_account_id": pool_account_id, "on_job_end": on_job_end},
        daemon=True,
    )
    thread.start()
    return job_id, None


@app.route("/ipindia/scrape", methods=["POST"])
def ipindia_scrape():
    """
    Step 3 submit: take OTP, run scrape in background.
    """
    email       = request.form.get("email", "").strip()
    query       = request.form.get("query", "").strip()
    search_type = request.form.get("search_type", "name").strip()
    pending_id  = request.form.get("pending_id", "").strip()
    otp_code    = request.form.get("otp_code", "").strip()

    errors = []
    if not email or "@" not in email:
        errors.append("Valid email address is required.")
    if not query:
        errors.append("Search query cannot be empty.")
    if search_type not in ("name", "appno"):
        search_type = "name"
    # Only require email IMAP credentials when we have no manually-entered OTP
    # and must fall back to automatic inbox polling.
    if not otp_code and not config.is_otp_configured():
        errors.append("Email OTP credentials not configured — go to Settings first.")

    if errors:
        return render_template(
            "ipindia.html",
            otp_configured=config.is_otp_configured(),
            error=" ".join(errors),
            message=None,
        )

    # Resolve pending session (from Step 1 CAPTCHA fetch & Step 2 OTP send)
    pending_sess = _pending_ip_sessions.pop(pending_id, None) if pending_id else None
    if not otp_code:
        pending_sess = None  # fall back to fully automatic mode

    # If App No, we need the search CAPTCHA solved before we can search.
    # This is now fully automatic (regex parser + Grok fallback) — no user
    # step required.
    if pending_sess and search_type == "appno":
        ok, err = pending_sess.verify_otp_only(otp_code)
        if not ok:
            return render_template("ipindia.html", error=f"OTP Verification Failed: {err}", step=3, pending_id=pending_id, email=email, query=query, search_type=search_type)

    job_id, launch_err = _launch_ipindia_job(email, query, search_type, pending_sess, otp_code, pending_id)
    if launch_err:
        return render_template("ipindia.html", error=launch_err, step=3, pending_id=pending_id, email=email, query=query, search_type=search_type)

    return render_template(
        "ipindia.html",
        otp_configured=config.is_otp_configured(),
        error=None,
        message=None,
        job_id=job_id,
        job_query=query,
    )

@app.route("/ipindia/search_another", methods=["POST"])
def ipindia_search_another():
    """
    Reuse an already-authenticated session (still holding a valid tab_token)
    to search a new Application Number without repeating login/OTP.
    Fetches a fresh search CAPTCHA, auto-solves it (regex + Grok fallback),
    and starts the search job directly — no user CAPTCHA step required.
    """
    email       = request.form.get("email", "").strip()
    query       = request.form.get("query", "").strip()
    search_type = request.form.get("search_type", "appno").strip()
    pending_id  = request.form.get("pending_id", "").strip()

    pending_sess = _pending_ip_sessions.get(pending_id) if pending_id else None
    if not pending_sess or not pending_sess.tab_token:
        return render_template(
            "ipindia.html",
            otp_configured=config.is_otp_configured(),
            error="Session expired — please log in again.", step=1,
        )

    if not query:
        return render_template(
            "ipindia.html",
            otp_configured=config.is_otp_configured(),
            error="Search query cannot be empty.", step=1,
        )

    prepare_data = pending_sess.prepare_search_appno()
    if not prepare_data:
        return render_template(
            "ipindia.html",
            otp_configured=config.is_otp_configured(),
            error="Session expired on the portal side — please log in again.", step=1,
        )

    if prepare_data.get("auto_answer") is None:
        return render_template(
            "ipindia.html",
            otp_configured=config.is_otp_configured(),
            error="Could not auto-solve search CAPTCHA (regex + Grok both failed). Check GROK_API_KEY in Settings.",
            step=1,
        )

    pending_sess.search_prepare_data = prepare_data
    _pending_ip_sessions[pending_id] = pending_sess

    job_id = uuid.uuid4().hex
    with _ipjobs_lock:
        _ipjobs[job_id] = {
            "status": "running",
            "step": "Starting…",
            "message": "",
            "results": [],
            "search_id": None,
            "query": query,
            "search_type": search_type,
        }

    thread = threading.Thread(
        target=_run_ipindia_job,
        args=(job_id, email, query, search_type, pending_sess, None, str(prepare_data["auto_answer"]), pending_id),
        daemon=True,
    )
    thread.start()

    return render_template(
        "ipindia.html",
        otp_configured=config.is_otp_configured(),
        error=None,
        message=None,
        job_id=job_id,
        job_query=query,
    )


def _parse_date_or_404(date_str: str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        abort(404)


@app.route("/working", methods=["GET"])
def working_list():
    """Applications the team is actively handling — separate from the raw
    scrape results in Search History. Kept distinct from the portal's own
    status vocabulary (Objected, Formalities Chk Pass, ...): this is an
    internal 'where are we with this client' tracker, not another mirror of
    IP India's status."""
    conn = db.get_connection()
    try:
        items = db.list_active_work_items(conn)
    finally:
        conn.close()
    return render_template("working.html", items=items, work_statuses=db.WORK_STATUSES)


@app.route("/working/start", methods=["POST"])
def working_start():
    """Called from a Search History card's 'Start Working' button — app_no
    comes from the card, not a form the user fills in."""
    app_no = request.form.get("app_no", "").strip()
    back = request.form.get("back", "").strip()
    if app_no:
        conn = db.get_connection()
        try:
            db.start_working(conn, app_no)
        finally:
            conn.close()
    return redirect(back or url_for("working_list"))


@app.route("/working/<int:work_item_id>/status", methods=["POST"])
def working_set_status(work_item_id):
    status = request.form.get("status", "").strip()
    if status in db.WORK_STATUSES:
        conn = db.get_connection()
        try:
            db.update_work_status(conn, work_item_id, status)
        finally:
            conn.close()
    return redirect(url_for("working_list"))


@app.route("/working/<int:work_item_id>/note", methods=["POST"])
def working_add_note(work_item_id):
    note = request.form.get("note", "").strip()
    if note:
        conn = db.get_connection()
        try:
            db.add_work_note(conn, work_item_id, note)
        finally:
            conn.close()
    return redirect(url_for("working_list"))


@app.route("/working/note/<int:note_id>/delete", methods=["POST"])
def working_delete_note(note_id):
    conn = db.get_connection()
    try:
        db.delete_work_note(conn, note_id)
    finally:
        conn.close()
    return redirect(url_for("working_list"))


@app.route("/working/<int:work_item_id>/close", methods=["POST"])
def working_close(work_item_id):
    conn = db.get_connection()
    try:
        db.close_work_item(conn, work_item_id)
    finally:
        conn.close()
    return redirect(url_for("working_list"))


@app.route("/ipindia/history", methods=["GET"])
def ipindia_history_dates():
    """
    Unified Search History: date range + status + applicant type + contact
    visibility + agent, all combinable via query params
    (?date_from=&date_to=&status=&applicant_type=&contact_visibility=&agent=).
    Date range/status/agent are filtered in SQL (db.search_ipindia_results);
    applicant type and contact visibility are classified in Python from
    free-text names, so they narrow the SQL-fetched set afterward — see
    ipindia_grouping.classify_applicant_type / classify_contact_visibility.
    """
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    status = request.args.get("status", "").strip()
    applicant_type = request.args.get("applicant_type", "").strip()
    contact_visibility = request.args.get("contact_visibility", "").strip()
    reply_status = request.args.get("reply_status", "").strip()
    agent = request.args.get("agent", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 40

    conn = db.get_connection()
    all_rows = db.search_ipindia_results(
        conn,
        date_from=date_from or None,
        date_to=date_to or None,
        status=status or None,
        agent_name=agent or None,
    )
    real_status_counts = {row["status"]: row["result_count"] for row in db.list_ipindia_status_counts(conn)}
    agents = db.list_tma_agent_counts(conn)
    working_app_nos = {item["app_no"] for item in db.list_active_work_items(conn)}
    conn.close()

    if applicant_type:
        all_rows = [r for r in all_rows if classify_applicant_type(r["proprietor_name"]) == applicant_type]
    if contact_visibility:
        all_rows = [
            r for r in all_rows
            if classify_contact_visibility(r["applicant_mobile"], r["applicant_email"]) == contact_visibility
        ]
    # Reply-status filter only makes sense once a specific Objected-like
    # status is selected — otherwise "reply to what?" is undefined, so the
    # dropdown itself is hidden client-side unless status == "Objected".
    if reply_status and status == "Objected":
        all_rows = [r for r in all_rows if classify_reply_status(r["query"]) == reply_status]

    total = len(all_rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    rows = all_rows[start:start + per_page]
    for r in rows:
        r["logo_filename"] = Path(r["logo_path"]).name if r.get("logo_path") else None
        r["reply_status"] = classify_reply_status(r["query"]) if r["status"] == "Objected" else None
        r["is_working"] = r["query"] in working_app_nos

    statuses = []
    seen = set()
    for name in _KNOWN_IPINDIA_STATUSES:
        statuses.append({"status": name, "result_count": real_status_counts.get(name, 0)})
        seen.add(name)
    for name, count in real_status_counts.items():
        if name not in seen:
            statuses.append({"status": name, "result_count": count})

    return render_template(
        "ipindia_history_dates.html",
        rows=rows, page=page, total_pages=total_pages, total=total,
        statuses=statuses, applicant_types=APPLICANT_TYPES,
        visibility_types=CONTACT_VISIBILITY_LABELS, reply_statuses=REPLY_STATUS_LABELS, agents=agents,
        f_date_from=date_from, f_date_to=date_to, f_status=status,
        f_applicant_type=applicant_type, f_contact_visibility=contact_visibility,
        f_reply_status=reply_status, f_agent=agent,
    )


@app.route("/ipindia/history/agent", methods=["GET"])
def ipindia_history_agents():
    """Standalone grid of every agent/advocate who has filed at least one
    application, each with a count — a dedicated browse page, separate from
    the unified filter's agent dropdown."""
    conn = db.get_connection()
    agents = db.list_tma_agent_counts(conn)
    conn.close()
    return render_template("ipindia_history_agents.html", agents=agents)


@app.route("/ipindia/history/agent/<path:agent_name>", methods=["GET"])
def ipindia_history_agent_detail(agent_name):
    """Applications filed by one agent (exact name match), paginated, plus
    the agent's own profile details and a trademark-category breakdown."""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 40
    conn = db.get_connection()
    rows, total = db.list_tma_documents_by_agent(conn, agent_name, limit=per_page, offset=(page - 1) * per_page)
    profile = db.get_tma_agent_profile(conn, agent_name)
    category_breakdown = db.get_tma_agent_category_breakdown(conn, agent_name)
    conn.close()
    if total == 0:
        abort(404)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    return render_template(
        "ipindia_history_agent.html",
        agent_name=agent_name, rows=rows, page=page, total_pages=total_pages, total=total,
        profile=profile, category_breakdown=category_breakdown,
    )


@app.route("/ipindia/history/<date_str>", methods=["GET"])
def ipindia_history_date(date_str):
    """Level 2: application-number ranges + individual (name) searches for one date."""
    _parse_date_or_404(date_str)
    conn = db.get_connection()
    rows = db.list_ipindia_searches_for_date(conn, date_str)
    conn.close()
    if not rows:
        abort(404)
    groups = group_searches_for_date(rows)
    return render_template("ipindia_history_date.html", date_str=date_str, groups=groups)


@app.route("/ipindia/history/<date_str>/range/<int:start>/<int:end>", methods=["GET"])
def ipindia_history_range(date_str, start, end):
    """
    Level 3: individual results within one previously-shown range. Group
    membership is re-derived server-side from the date's current rows
    (nothing about the range is persisted) — if new searches landed in that
    date since the Level 2 page was loaded, the range boundaries could shift
    slightly; acceptable for a single-user local app.
    """
    _parse_date_or_404(date_str)
    conn = db.get_connection()
    rows = db.list_ipindia_searches_for_date(conn, date_str)
    conn.close()
    if not rows:
        abort(404)

    groups = group_searches_for_date(rows)
    match = next(
        (g for g in groups if g["kind"] == "range" and g["start"] == start and g["end"] == end),
        None,
    )
    if match is None:
        abort(404)

    id_set = set(match["search_ids"])
    range_rows = [r for r in rows if r["id"] in id_set]

    page = max(1, request.args.get("page", 1, type=int))
    per_page = 40
    total = len(range_rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start_idx = (page - 1) * per_page
    page_rows = range_rows[start_idx:start_idx + per_page]

    return render_template(
        "ipindia_history_range.html",
        date_str=date_str, start=start, end=end, rows=page_rows,
        page=page, total_pages=total_pages, total=total,
    )


# Internal login-flow progress text (see ipindia_scraper.py's status()
# callbacks — "OTP sent to {account email}...", "Verifying OTP...", etc.)
# is useful for operator debugging but must never reach an end user: it
# names real pool-account email addresses and portal-login mechanics that
# are none of a client's business. Rather than an allowlist (too easy to
# under-cover a legitimate phrase, as a first pass here did), this blocks
# anything that looks like an email address or mentions the specific
# login-flow vocabulary, and passes through everything else unchanged.
_UNSAFE_STATUS_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.-]+|\bOTP\b|\block(?:ed|ing)?\s*(in|out)\b|\bcaptcha\b|\bpassword\b|\baccount\b|\bpool\b|\binbox\b",
    re.IGNORECASE,
)


def _sanitize_status_text(text: str | None) -> str:
    if not text:
        return ""
    if _UNSAFE_STATUS_RE.search(text):
        return "Searching…"
    return text


@app.route("/ipindia/status/<job_id>", methods=["GET"])
def ipindia_status(job_id):
    with _ipjobs_lock:
        job = _ipjobs.get(job_id)
    if job is None:
        # In-memory entry gone (scraper restarted mid-job, or an old job) —
        # fall back to the persistent scrape_jobs table so pollers still get
        # a real answer instead of spinning on "unknown".
        try:
            conn = db.get_connection()
            try:
                row = db.get_scrape_job(conn, job_id)
            finally:
                conn.close()
        except Exception:
            row = None
        if row is None:
            return jsonify({"status": "unknown"}), 404
        return jsonify({
            "status": row["status"],
            "step": _sanitize_status_text(row.get("step")),
            "message": _sanitize_status_text(row.get("error")),
            "search_id": row.get("search_id"),
            "result_count": row.get("result_count") or 0,
            "pending_id": None,
            "email": None,
        })
    # Don't send full results list in status — just metadata
    return jsonify({
        "status": job["status"],
        "step":   _sanitize_status_text(job.get("step")),
        "message": _sanitize_status_text(job.get("message")),
        "search_id": job.get("search_id"),
        "result_count": len(job.get("results", [])),
        "pending_id": job.get("pending_id"),
        "email": None,  # never surface the scraper's own login-account address
        # Live document-saving progress (see _run_ipindia_job) — "none" (no
        # documents on this trademark), "pending"/"saving" (still fetching,
        # more may appear), or "done". `documents` grows across polls as
        # each PDF finishes saving, so the UI can show download links one
        # by one instead of only after everything is done.
        "docs_status": job.get("docs_status", "none"),
        "documents": job.get("documents", []),
        "docs_message": job.get("docs_message"),
    })


_SCRAPE_APPNO_MAX_ATTEMPTS = 6           # keep retrying claim+login through a
                                          # transient portal outage (401s on
                                          # GetCaptcha, pool exhaustion, etc.)
                                          # instead of giving up after one try
_SCRAPE_APPNO_RETRY_BACKOFF_SECONDS = 20 # gap between attempts — long enough
                                          # not to hammer a struggling/down
                                          # government portal

# Errors that mean "this account is cooling down on IP India's side" — an
# immediate retry on the SAME account is guaranteed to fail the same way, so
# back off long enough for the portal's ~90s block to clear.
_COOLDOWN_ERROR_MARKERS = (
    "already logged in on another tab",
    "previous logout was not clean",
    "otp already sent",
    "please wait",
    "portal is currently blocking this machine",
)
_SCRAPE_APPNO_COOLDOWN_BACKOFF_SECONDS = 100


def _is_cooldown_error(message: str) -> bool:
    m = (message or "").lower()
    return any(mark in m for mark in _COOLDOWN_ERROR_MARKERS)


def _claim_and_launch_with_retry(job_id: str, app_no: str, search_type: str) -> None:
    """Background body of /api/scrape-appno: retries claim+authenticate+
    launch through transient failures (portal 401s, no free pool account)
    instead of failing the job on the first hiccup — the job entry already
    exists (status='running') before this starts, so pollers see live retry
    progress via 'step' rather than an immediate error.

    appno searches try a warm session first (see warm_pool.py) — if one's
    ready, this skips straight to launching with it (no CAPTCHA/OTP login
    at all) and only falls through to the normal cold-claim retry loop
    below if no warm slot was available. Name search is unaffected — warm
    slots aren't tracked per search_type, and TM Search (the only current
    caller of search_type='name') never goes through this warm path.

    Counts itself in _active_search_jobs for the duration of the actual
    search job (from here until _run_ipindia_job finishes) so
    _auto_launch_loop can hold off starting new series jobs while a live
    TM Search is in flight — a live user-facing search shouldn't have to
    queue up behind the background scanner's own CAPTCHA/OTP logins on the
    same shared pool.
    """
    with _active_search_jobs_lock:
        global _active_search_jobs
        _active_search_jobs += 1

    def _decrement() -> None:
        with _active_search_jobs_lock:
            global _active_search_jobs
            _active_search_jobs -= 1

    # handed_off becomes True only once a background job has actually taken
    # ownership of decrementing (via on_job_end, fired when that job's run
    # truly finishes) — every other path (all retries exhausted, or an
    # unexpected exception) must decrement itself here instead, since
    # nothing else ever will.
    handed_off = False
    try:
        if search_type == "appno":
            warm_slot = warm_pool.claim_warm_slot()
            if warm_slot is not None:
                with _ipjobs_lock:
                    _ipjobs[job_id]["step"] = "Searching (warm session)…"

                def _on_warm_job_end(exc: Exception | None) -> None:
                    warm_pool.release_and_refill(warm_slot, died=isinstance(exc, SessionExpiredError))
                    _decrement()

                launched_job_id, launch_err = _launch_ipindia_job(
                    warm_slot.pool_account["email_address"], app_no, search_type, warm_slot.sess,
                    otp_code=None, pending_id=None, pool_account_id=None,
                    existing_job_id=job_id, on_job_end=_on_warm_job_end,
                )
                if not launch_err:
                    handed_off = True
                    return
                # Warm session couldn't even start the search-CAPTCHA step
                # (prepare_search_appno failed) — this isn't necessarily the
                # session's fault (could be a portal-wide CAPTCHA hiccup), so
                # release it back to 'ready' rather than assuming it's dead;
                # fall through to the normal cold-path retry loop below for
                # this job instead of failing it outright.
                logging.warning("scrape-appno: warm slot launch failed (%s) — falling back to cold pool", launch_err)
                warm_pool.release_and_refill(warm_slot, died=False)

        last_err = "Unknown error."
        for attempt in range(1, _SCRAPE_APPNO_MAX_ATTEMPTS + 1):
            with _ipjobs_lock:
                _ipjobs[job_id]["step"] = (
                    f"Connecting to IP India (attempt {attempt}/{_SCRAPE_APPNO_MAX_ATTEMPTS})…"
                )
            claim_job_id = uuid.uuid4().hex
            sess, pool_account, err = _pool_claim_and_authenticate(claim_job_id)
            if err:
                last_err = err
                logging.warning("scrape-appno attempt %d/%d failed: %s", attempt, _SCRAPE_APPNO_MAX_ATTEMPTS, err)
                if attempt < _SCRAPE_APPNO_MAX_ATTEMPTS:
                    backoff = (
                        _SCRAPE_APPNO_COOLDOWN_BACKOFF_SECONDS
                        if _is_cooldown_error(err)
                        else _SCRAPE_APPNO_RETRY_BACKOFF_SECONDS
                    )
                    with _ipjobs_lock:
                        _ipjobs[job_id]["step"] = (
                            f"IP India accounts cooling down — retrying in {backoff}s "
                            f"(attempt {attempt}/{_SCRAPE_APPNO_MAX_ATTEMPTS})…"
                            if _is_cooldown_error(err)
                            else f"Connecting to IP India (attempt {attempt + 1}/{_SCRAPE_APPNO_MAX_ATTEMPTS})…"
                        )
                    time.sleep(backoff)
                continue

            # _launch_ipindia_job spawns its own thread and takes over this
            # job_id's lifecycle (running -> done/error) from here — success
            # means handing off, not finishing this function successfully.
            launched_job_id, launch_err = _launch_ipindia_job(
                pool_account["email_address"], app_no, search_type, sess,
                otp_code=None, pending_id=None, pool_account_id=pool_account["id"],
                existing_job_id=job_id, on_job_end=lambda exc: _decrement(),
            )
            if not launch_err:
                handed_off = True
                return
            last_err = launch_err
            conn = db.get_connection()
            try:
                db.release_pool_account(conn, pool_account["id"])
            finally:
                conn.close()
            logging.warning("scrape-appno attempt %d/%d failed at launch: %s", attempt, _SCRAPE_APPNO_MAX_ATTEMPTS, launch_err)
            if attempt < _SCRAPE_APPNO_MAX_ATTEMPTS:
                time.sleep(_SCRAPE_APPNO_RETRY_BACKOFF_SECONDS)

        with _ipjobs_lock:
            _ipjobs[job_id].update(status="error", message=last_err)
    finally:
        if not handed_off:
            _decrement()


@app.route("/api/scrape-appno", methods=["POST"])
def api_scrape_appno():
    """
    JSON API for other local apps (e.g. tm_manager) to request a fully
    automatic scrape of one application number OR brand/word mark — claims a
    free pool account, logs in, and searches, exactly like
    ipindia_get_captcha's pool-mode branch does for a human clicking "Get
    CAPTCHA" on /ipindia. Fire-and-forget: returns a job_id immediately; poll
    /api/scrape-status/<job_id> for the outcome — the job itself retries
    through transient portal failures (see _claim_and_launch_with_retry)
    rather than failing on the first attempt.

    Body: {"app_no": "1234567", "search_type": "appno"|"name"} — search_type
          defaults to "appno" for backward compatibility with callers that
          only ever passed app_no.
    Returns: {"job_id": "..."} , 202
         or: {"error": "..."} , 400 (bad input only now — transient failures
             are retried in the background instead of surfacing here)
    """
    payload = request.get_json(silent=True) or {}
    app_no = str(payload.get("app_no", "")).strip()
    search_type = str(payload.get("search_type", "appno")).strip()
    if search_type not in ("appno", "name"):
        search_type = "appno"
    if not app_no:
        return jsonify({"error": "app_no is required."}), 400

    requested_by = str(payload.get("requested_by", "")).strip() or None

    job_id = uuid.uuid4().hex
    with _ipjobs_lock:
        _ipjobs[job_id] = {
            "status": "running",
            "step": "Starting…",
            "message": "",
            "results": [],
            "search_id": None,
            "query": app_no,
            "app_no": app_no,
            "search_type": search_type,
        }
    # Persist the job so /api/scrape-status still answers after a scraper
    # restart, and a poller can find a job it started.
    try:
        conn = db.get_connection()
        try:
            db.create_scrape_job(conn, job_id, app_no, search_type, requested_by)
        finally:
            conn.close()
    except Exception:
        logging.exception("create_scrape_job failed for %s", job_id)

    threading.Thread(
        target=_claim_and_launch_with_retry,
        args=(job_id, app_no, search_type),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id}), 202


@app.route("/api/scrape-status/<job_id>", methods=["GET"])
def api_scrape_status(job_id):
    """Thin, stable alias of ipindia_status for external callers — kept
    separate from the UI-internal route so tm_manager's contract doesn't
    silently change if /ipindia/status's shape ever evolves for its own UI."""
    return ipindia_status(job_id)


@app.route("/api/scrape-status", methods=["GET"])
def api_scrape_status_bulk():
    """Bulk status for a poller: ?ids=uuid,uuid,uuid -> {"jobs":[...]}.
    Reads the persistent scrape_jobs table (with a live-dict override), so it
    keeps working across a scraper restart."""
    ids = [x for x in (request.args.get("ids", "").split(",")) if x]
    if not ids:
        return jsonify({"jobs": []})
    conn = db.get_connection()
    try:
        rows = {r["id"]: r for r in db.get_scrape_jobs(conn, ids)}
    finally:
        conn.close()
    out = []
    for jid in ids:
        with _ipjobs_lock:
            live = _ipjobs.get(jid)
        if live is not None:
            out.append({
                "job_id": jid,
                "status": live["status"],
                "step": _sanitize_status_text(live.get("step")),
                "message": _sanitize_status_text(live.get("message")),
                "search_id": live.get("search_id"),
                "result_count": len(live.get("results", [])),
            })
        elif jid in rows:
            r = rows[jid]
            out.append({
                "job_id": jid,
                "status": r["status"],
                "step": _sanitize_status_text(r.get("step")),
                "message": _sanitize_status_text(r.get("error")),
                "search_id": r.get("search_id"),
                "result_count": r.get("result_count") or 0,
            })
        else:
            out.append({"job_id": jid, "status": "unknown"})
    return jsonify({"jobs": out})


# ── Warm session pool + pool-account role management ────────────────────────

@app.route("/api/warm-pool/status", methods=["GET"])
def api_warm_pool_status():
    return jsonify({"health": warm_pool.health(), "slots": warm_pool.status_snapshot()})


@app.route("/api/vpn/status", methods=["GET"])
def api_vpn_status():
    """Current VPN rotation state — which server (if any) is connected,
    and every known server's usage/block state. Lets a human (or a script
    run from a non-elevated shell) check and drive VPN rotation THROUGH
    this service's own SYSTEM-level permissions instead of needing their
    own elevated session to call wireguard.exe directly — see
    vpn_rotator.py's module docstring for why that matters (this process
    runs as a Windows service under LocalSystem specifically so VPN
    connect/switch never needs an interactive UAC prompt)."""
    servers = vpn_rotator.list_servers()
    state = vpn_rotator._load_state()
    return jsonify({
        "active": vpn_rotator.current_server(),
        "servers": [
            {"name": name, **state.get(name, {"usage_seconds": 0.0, "blocked_until": 0.0, "last_connected_at": 0.0})}
            for name in servers
        ],
    })


@app.route("/api/vpn/connect", methods=["POST"])
def api_vpn_connect():
    """Connect the least-used eligible VPN server (or a specific one via
    ?name=...). Runs inside THIS process — which is why it needs to be a
    route at all: calling wireguard.exe from an unprivileged shell prompts
    for UAC every time, but this service already runs as LocalSystem, so
    the exact same call made from here needs no prompt at all.

    Also refreshes local_store's pool-account cache from Contabo (while
    it's still reachable, i.e. BEFORE the tunnel comes up) and flips
    local_store.LOCAL_MODE on — see local_store.py's module docstring for
    why scraping needs to go fully local-only rather than
    try-Contabo-then-fall-back once VPN is active (that pattern is what
    stalled/killed series job b34ed6c31fae459a9670dc792ed4d100 earlier
    tonight)."""
    try:
        conn = db.get_connection()
        try:
            accounts = db.list_pool_accounts(conn)
        finally:
            conn.close()
        local_store.refresh_pool_cache(accounts)
        local_store.release_all_claims_local()
    except Exception:
        logging.exception("api_vpn_connect: could not refresh pool cache from Contabo before "
                           "connecting — proceeding with whatever local_store already has cached.")

    name = request.args.get("name") or vpn_rotator.pick_next_server()
    if not name:
        return jsonify({"ok": False, "error": "No eligible VPN server (all blocked or none configured)."}), 409
    ok = vpn_rotator.switch_to(name)
    if ok:
        local_store.set_local_mode(True)
    return jsonify({"ok": ok, "server": name if ok else None, "local_mode": local_store.LOCAL_MODE})


@app.route("/api/vpn/disconnect", methods=["POST"])
def api_vpn_disconnect():
    """Disconnect whatever VPN server is currently active, if any, and
    turn local_store.LOCAL_MODE back off — new scrape jobs started after
    this go straight to Contabo again, same as before VPN rotation
    existed."""
    cur = vpn_rotator.current_server()
    if cur:
        vpn_rotator.disconnect(cur)
    local_store.set_local_mode(False)
    return jsonify({"ok": True, "disconnected": cur})


@app.route("/api/vpn/local-progress", methods=["GET"])
def api_vpn_local_progress():
    """How much is sitting in the local store right now, waiting to sync
    to Contabo — the 'how much done / not done' overnight tracker. Safe to
    poll from a browser tab left open all night; makes zero Contabo calls
    itself (reads local_store's own SQLite file only)."""
    return jsonify({
        "local_mode": local_store.LOCAL_MODE,
        "pending_hits_not_yet_synced": local_store.pending_hit_count(),
    })


@app.route("/api/worker-status", methods=["GET"])
def api_worker_status():
    """Live per-account session status for this worker machine — which
    account is fetching right now, and cumulative success/failure counts
    for this process's lifetime (see session_tracker.py). JSON counterpart
    of /status."""
    return jsonify({
        "machine_key": config.FLEET_MACHINE_KEY or None,
        "fleet_mode_enabled": config.FLEET_MODE_ENABLED,
        "warm_pool": {"health": warm_pool.health(), "slots": warm_pool.status_snapshot()},
        "sessions": session_tracker.snapshot(),
        "fleet_today": _fleet_today_progress(),
    })


@app.route("/status", methods=["GET"])
def status_page():
    """Local, in-browser status page for this worker machine — see
    session_tracker.py's module docstring for what "session" counts mean."""
    return render_template(
        "status.html",
        machine_key=config.FLEET_MACHINE_KEY or "(unset)",
        fleet_mode_enabled=config.FLEET_MODE_ENABLED,
        warm_health=warm_pool.health(),
        warm_slots=warm_pool.status_snapshot(),
        session_data=session_tracker.snapshot(),
        fleet_today=_fleet_today_progress(),
    )


_BATCH_MAX_APP_NOS = 50            # hard cap per request — keep one HTTP call bounded
_BATCH_DELAY_MIN = 1.0             # politeness jitter between searches, same idea as
_BATCH_DELAY_MAX = 2.5             # Auto Discover's delay_min/delay_max


_FLEET_REFILL_BATCH_SIZE = 15  # how many more app_nos to ask tm_manager for, per refill call


def _claim_more_from_tm_manager(machine_id: int, batch_size: int) -> list[str]:
    """Ask tm_manager's /api/fleet/claim-more for another chunk of this
    machine's assigned work, on the SAME login session that's already
    running — see _run_batch_job's docstring for why this exists. Returns
    [] on any failure or when there's genuinely nothing left to claim; the
    caller treats an empty list as "this session is done, log out
    normally," never as an error."""
    try:
        resp = requests.post(
            f"{config.TM_MANAGER_BASE_URL}/api/fleet/claim-more",
            json={"machine_id": machine_id, "batch_size": batch_size},
            auth=(
                (config.TM_MANAGER_OPS_USER, config.TM_MANAGER_OPS_PASS)
                if config.TM_MANAGER_OPS_USER and config.TM_MANAGER_OPS_PASS else None
            ),
            timeout=15,
        )
        if resp.status_code != 200:
            logging.warning("Fleet: claim-more declined (HTTP %s): %s", resp.status_code, resp.text[:200])
            return []
        return resp.json().get("app_nos") or []
    except requests.RequestException as exc:
        logging.warning("Fleet: claim-more unreachable: %s", exc)
        return []


def _report_result_to_tm_manager(
    machine_id: int, app_no: str, outcome: str, search_id: int | None, message: str,
) -> None:
    """Companion to _claim_more_from_tm_manager — tm_manager's own poll loop
    (scheduler.py's _run_batch) only applies results for the app_nos it
    originally sent in the batch request, so an item claimed mid-session via
    /api/fleet/claim-more needs its outcome reported back explicitly or it
    stays stuck at status='fetching'. Best-effort: a failure here just means
    that one row waits for the stale-fetching reaper instead of updating
    immediately, never a fetch-correctness problem (the scrape result itself
    is already durably saved via save_ipindia_search before this is called)."""
    try:
        resp = requests.post(
            f"{config.TM_MANAGER_BASE_URL}/api/fleet/report-result",
            json={
                "machine_id": machine_id, "app_no": app_no, "outcome": outcome,
                "search_id": search_id, "message": message,
            },
            auth=(
                (config.TM_MANAGER_OPS_USER, config.TM_MANAGER_OPS_PASS)
                if config.TM_MANAGER_OPS_USER and config.TM_MANAGER_OPS_PASS else None
            ),
            timeout=15,
        )
        if resp.status_code != 200:
            logging.warning("Fleet: report-result declined for %s (HTTP %s): %s", app_no, resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        logging.warning("Fleet: report-result unreachable for %s: %s", app_no, exc)


def _run_batch_job(job_id: str, app_nos: list[str], requested_by: str | None) -> None:
    """Background body of /api/scrape-appno-batch — the TM-Manager-queue
    equivalent of Auto Discover's _run_discover_job: claim ONE warm session
    and hold it back-to-back for as long as it stays alive, exactly like
    Auto Discover's own loop — not just for the initial app_nos list. Once
    that list runs out, and this is a fleet-mode machine, it calls
    tm_manager's /api/fleet/claim-more for another chunk on the SAME
    session instead of logging out — a fixed AUTO_FETCH_BATCH_SIZE used to
    force a fresh cold login (and the account's ~120s post-logout cooldown)
    every N marks even though the session itself was still perfectly
    healthy, which was pure wasted overhead compared to how Auto Discover
    runs. The loop only stops when the session actually dies
    (SessionExpiredError), a refill comes back empty (no more assigned
    work), or a safety cap trips (_MAX_TOTAL_ATTEMPTS/_MAX_CONSECUTIVE_ERRORS,
    same constants Auto Discover itself uses).

    Falls back to the normal cold pool (one claim per batch, not per
    number) if no warm slot is available, so a batch never blocks forever
    just because every warm lane is busy.
    """
    with _batch_jobs_lock:
        _batch_jobs[job_id] = {
            "status": "running", "total": len(app_nos), "done": 0,
            "results": {}, "started_at": time.time(),
        }

    original_app_nos = set(app_nos)  # tm_manager's own poll loop already applies results for
                                      # these via status["results"] — only items claimed later
                                      # through /api/fleet/claim-more need the extra report call,
                                      # or they'd get double-applied.
    fleet_machine_id = _resolve_fleet_machine_id() if config.FLEET_MODE_ENABLED else None

    def _set_result(app_no: str, outcome: str, search_id: int | None, message: str) -> None:
        with _batch_jobs_lock:
            job = _batch_jobs.get(job_id)
            if job is None:
                return
            job["results"][app_no] = {"outcome": outcome, "search_id": search_id, "message": message}
            job["done"] += 1
        if fleet_machine_id is not None and app_no not in original_app_nos:
            _report_result_to_tm_manager(fleet_machine_id, app_no, outcome, search_id, message)

    # exclude_priority=True: this is the batch auto-fetch path — it must
    # never claim a priority-reserved warm slot (see warm_pool.WarmSlot.
    # is_priority), so a single manual TM Search started mid-batch always
    # has at least one warm account free instead of falling back to a slow
    # cold login just because a big batch happens to be running.
    warm_slot = warm_pool.claim_warm_slot(exclude_priority=True)
    sess = None
    email = None
    pool_account_id = None
    died = False

    try:
        if warm_slot is not None:
            sess = warm_slot.sess
            email = warm_slot.pool_account["email_address"]
            session_tracker.start_session(email, job_id)
        else:
            # No warm lane free — fall back to ONE cold login for the whole
            # batch (still far better than one cold login per mark). Retries
            # through transient failures exactly like the single-item path
            # (_claim_and_launch_with_retry) does — a bare single attempt
            # here would report the WHOLE batch as failed on one hiccup
            # (e.g. "no free pool account" while accounts are mid-login),
            # and the caller would immediately start a fresh batch a few
            # seconds later, racing new login attempts against the accounts
            # that were already mid-cooldown from THIS attempt. That retry
            # storm is exactly what happened before this fix (2026-09-11).
            pool_account = None
            err = "Unknown error."
            for attempt in range(1, _SCRAPE_APPNO_MAX_ATTEMPTS + 1):
                claim_job_id = uuid.uuid4().hex
                sess, pool_account, err = _pool_claim_and_authenticate(claim_job_id, for_batch_worker=True)
                if sess is not None:
                    break
                if attempt < _SCRAPE_APPNO_MAX_ATTEMPTS:
                    backoff = (
                        _SCRAPE_APPNO_COOLDOWN_BACKOFF_SECONDS
                        if _is_cooldown_error(err)
                        else _SCRAPE_APPNO_RETRY_BACKOFF_SECONDS
                    )
                    with _batch_jobs_lock:
                        job = _batch_jobs.get(job_id)
                        if job is not None:
                            job["step"] = f"Connecting (attempt {attempt}/{_SCRAPE_APPNO_MAX_ATTEMPTS}, retrying in {backoff}s)…"
                    time.sleep(backoff)
            if sess is None:
                for app_no in app_nos:
                    _set_result(app_no, "error", None, err or "Could not authenticate.")
                with _batch_jobs_lock:
                    _batch_jobs[job_id]["status"] = "done"
                return
            email = pool_account["email_address"]
            pool_account_id = pool_account["id"]
            session_tracker.start_session(email, job_id)

        queue = list(app_nos)
        total_attempts = 0
        consecutive_errors = 0

        while queue:
            app_no = queue.pop(0)
            total_attempts += 1
            session_tracker.set_current_appno(email, str(app_no))
            try:
                outcome, result = sess.search_appno_classified(str(app_no))
            except SessionExpiredError:
                died = True
                _set_result(app_no, "error", None, "Session expired mid-batch.")
                break  # session is dead — nothing left in queue gets a session either
            except Exception as exc:
                _set_result(app_no, "error", None, str(exc))
                consecutive_errors += 1
                time.sleep(random.uniform(_BATCH_DELAY_MIN, _BATCH_DELAY_MAX))
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS or total_attempts >= _MAX_TOTAL_ATTEMPTS:
                    break
                continue
            consecutive_errors = 0

            if outcome == SearchOutcome.ERROR:
                _set_result(app_no, "error", None, "Search failed (CAPTCHA/network/portal).")
            elif outcome == SearchOutcome.MISS:
                conn = db.get_connection()
                try:
                    # "appno_batch" (not "appno") — this is the TM Manager
                    # auto-fetch queue, not a human typing into TM Search.
                    # Keeping it distinct from "appno" lets TM Search's
                    # History list show only genuine manual searches (see
                    # tm_manager/db.py's list_recent_appno_searches) while
                    # every "does this app_no have cached data" lookup
                    # elsewhere still treats appno/appno_discover/appno_batch
                    # as equally valid cached data (see the IN (...) checks).
                    search_id = db.save_ipindia_search(conn, str(app_no), "appno_batch", email, [], error="No results found.")
                finally:
                    conn.close()
                _set_result(app_no, "miss", search_id, "No results found.")
            else:  # HIT
                view_token = result.pop("_view_token", None) if isinstance(result, dict) else None
                rvt = result.pop("_request_verification_token", None) if isinstance(result, dict) else None
                if isinstance(result, dict):
                    result.pop("_raw_html", None)
                conn = db.get_connection()
                try:
                    search_id = db.save_ipindia_search(conn, str(app_no), "appno_batch", email, [result], error="")
                finally:
                    conn.close()
                if view_token:
                    # Synchronous, same as the single-item path (_run_ipindia_job
                    # etc.) — this used to fire in a background thread capped at
                    # join(timeout=15), so a batch item was already reported as
                    # "hit" (and tm_manager marked it 'fetched') before we even
                    # knew whether the PDF finished downloading/parsing. Any
                    # document that took longer than 15s (multiple PDFs, a slow
                    # portal response) silently never made it into tma_documents,
                    # even though the file itself downloaded fine — the record
                    # then permanently shows blank Application Date/Class/etc.
                    # even though the source PDF is sitting right there.
                    #
                    # Now fetches Documents/Reminders/Correspondence/PR
                    # Details/e-main-page/Image all concurrently instead of
                    # just Documents alone — batch auto-fetch used to skip
                    # reminders/correspondence entirely.
                    #
                    # Skipped entirely if the portal Status is unchanged since
                    # the last fetch — see _should_skip_panels_fetch. This is
                    # what makes the daily staleness-refresh cheap: most
                    # re-checks confirm nothing changed and never touch
                    # documents/reminders/correspondence at all.
                    new_status = result.get("Status") if isinstance(result, dict) else None
                    skip_conn = db.get_connection()
                    try:
                        skip_panels = _should_skip_panels_fetch(skip_conn, str(app_no), search_id, new_status)
                    finally:
                        skip_conn.close()
                    if not skip_panels:
                        try:
                            _fetch_all_tm_panels(sess, str(app_no), view_token, rvt, search_id=search_id)
                        except Exception:
                            logging.exception("Failed to auto-save TM panels for %s", app_no)
                _set_result(app_no, "hit", search_id, "OK")

            # Queue ran dry but the session is still alive — ask tm_manager
            # for more of this machine's assigned work instead of ending
            # the session here, so one login covers as much as Auto
            # Discover's own back-to-back loop would.
            if not queue and fleet_machine_id is not None and total_attempts < _MAX_TOTAL_ATTEMPTS:
                more = _claim_more_from_tm_manager(fleet_machine_id, _FLEET_REFILL_BATCH_SIZE)
                if more:
                    queue.extend(more)
                    with _batch_jobs_lock:
                        job = _batch_jobs.get(job_id)
                        if job is not None:
                            job["total"] += len(more)

            time.sleep(random.uniform(_BATCH_DELAY_MIN, _BATCH_DELAY_MAX))

    except Exception as exc:
        logging.exception("Batch scrape job %s failed", job_id)
        with _batch_jobs_lock:
            job = _batch_jobs.get(job_id)
            if job is not None:
                job["error"] = str(exc)
    finally:
        if email is not None:
            session_tracker.end_session(email, success=not died)
        if warm_slot is not None:
            warm_pool.release_and_refill(warm_slot, died=died)
        elif pool_account_id is not None:
            conn = db.get_connection()
            try:
                db.release_pool_account(conn, pool_account_id)
            finally:
                conn.close()
        with _batch_jobs_lock:
            job = _batch_jobs.get(job_id)
            if job is not None:
                job["status"] = "done"


@app.route("/api/scrape-appno-batch", methods=["POST"])
def api_scrape_appno_batch():
    """Batch version of /api/scrape-appno — one HTTP call, one held session,
    many app numbers processed in a direct loop (see _run_batch_job). This is
    what tm_manager's auto-fetch queue should call instead of hitting
    /api/scrape-appno once per mark, so its throughput matches Auto
    Discover's instead of paying a full claim/release + 5s-poll cycle per
    trademark.

    Body: {"app_nos": ["123...", "456..."], "requested_by": "tm_manager"}
    Returns: {"job_id": "..."}, 202 — poll /api/scrape-appno-batch/<job_id>
    """
    payload = request.get_json(silent=True) or {}
    app_nos_raw = payload.get("app_nos") or []
    app_nos = [str(a).strip() for a in app_nos_raw if str(a).strip()]
    if not app_nos:
        return jsonify({"error": "app_nos is required (non-empty list)."}), 400
    if len(app_nos) > _BATCH_MAX_APP_NOS:
        return jsonify({"error": f"Max {_BATCH_MAX_APP_NOS} app_nos per batch."}), 400

    job_id = uuid.uuid4().hex
    threading.Thread(
        target=_run_batch_job,
        args=(job_id, app_nos, payload.get("requested_by")),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id}), 202


@app.route("/api/scrape-appno-batch/<job_id>", methods=["GET"])
def api_scrape_appno_batch_status(job_id):
    with _batch_jobs_lock:
        job = _batch_jobs.get(job_id)
        if job is None:
            return jsonify({"status": "unknown"}), 404
        return jsonify(dict(job))


_wordmark_jobs: dict[int, dict] = {}   # search_id -> {"step": "..."} (ephemeral, see _run_wordmark_search_job)
_wordmark_jobs_lock = threading.Lock()

# Shared, reused tmrpublicsearch session — the whole point is that only the
# FIRST search pays the ~60-90s cold-login cost; every search after that
# reuses this same logged-in session instantly. Only ever ONE at a time
# (tmrpublicsearch is a separate portal from eStatus with no warm-pool
# infra of its own yet — see warm_pool.py for the fuller multi-account
# version of this idea, not built here since one shared session is enough
# for how this feature is actually used today). Protected by a lock since
# multiple wordmark searches could be submitted close together.
_wm_session: "TmrPublicSearchSession | None" = None
_wm_session_pool_account_id: int | None = None
_wm_session_lock = threading.Lock()

# Separate from _wm_session_lock on purpose: that one only guards reading/
# creating the shared session object, held briefly. This one guards actual
# USE of that session for a search — search_wordmark() makes several HTTP
# calls against sess._http (CAPTCHA fetch, then the search POST) that are
# NOT safe to run from two threads at once on the same requests.Session
# (cookies/state get corrupted, and IP India's own per-tab CAPTCHA state
# gets confused). Without this, two Word Mark searches submitted close
# together would both grab the same cached session and race each other —
# observed live 2026-09-14: two rapid "RAM" searches both hung forever with
# no error, because they were silently fighting over the same session's
# HTTP state instead of one queuing cleanly behind the other.
_wm_search_lock = threading.Lock()


def _wm_get_or_login_session(on_status) -> tuple["TmrPublicSearchSession | None", str]:
    """Return the shared session if it's already logged in, else claim a
    pool account and log in fresh (retrying through transient failures,
    same cooldown-aware backoff as the eStatus single-search path). The
    account stays claimed (never released back to the cold pool) for as
    long as this session stays alive — see _wm_release_session for the only
    path that gives it back."""
    global _wm_session, _wm_session_pool_account_id

    with _wm_session_lock:
        if _wm_session is not None:
            return _wm_session, ""

        # Internal retry/attempt/backoff bookkeeping is deliberately NOT
        # surfaced to on_status — a user watching this should see a single
        # calm "Connecting…" the whole time, not "attempt 5/6, retrying in
        # 20s…". Attempt numbers and backoff timing are implementation
        # detail; only sess.authenticate's own genuine progress messages
        # (CAPTCHA solved, waiting for OTP, etc.) and a clean final error
        # (if every attempt truly fails) should ever reach the UI.
        on_status("Connecting…")
        err = "Unknown error."
        for attempt in range(1, _SCRAPE_APPNO_MAX_ATTEMPTS + 1):
            conn = db.get_connection()
            try:
                claim_job_id = uuid.uuid4().hex
                # Prefer an account dedicated to Word Mark search (role=
                # 'wordmark' in the Email Pool settings) so this doesn't
                # compete with app-number search / batch auto-fetch for the
                # same shared accounts — falls back to the regular cold pool
                # if none are assigned, so this keeps working with zero
                # configuration either way.
                pool_account = db.claim_wordmark_pool_account(conn, claim_job_id)
                if pool_account is None:
                    pool_account = db.claim_free_pool_account(conn, claim_job_id, machine_id=_resolve_fleet_machine_id())
            finally:
                conn.close()
            if pool_account is None:
                err = "No free email pool account available right now."
            else:
                sess = TmrPublicSearchSession(pool_account)
                ok, err = sess.authenticate(on_status=on_status)
                if ok:
                    _wm_session = sess
                    _wm_session_pool_account_id = pool_account["id"]
                    return _wm_session, ""
                conn = db.get_connection()
                try:
                    db.release_pool_account(conn, pool_account["id"])
                finally:
                    conn.close()
            if attempt < _SCRAPE_APPNO_MAX_ATTEMPTS:
                backoff = (
                    _SCRAPE_APPNO_COOLDOWN_BACKOFF_SECONDS
                    if _is_cooldown_error(err)
                    else _SCRAPE_APPNO_RETRY_BACKOFF_SECONDS
                )
                time.sleep(backoff)
        return None, err


def _wm_release_session(died: bool) -> None:
    """Called when the shared session turns out to be dead (search raised —
    treated as a session/auth failure) so the NEXT search re-logs in fresh
    instead of reusing a broken session forever. A normal successful search
    does NOT call this — the session stays cached for the next search."""
    global _wm_session, _wm_session_pool_account_id
    if not died:
        return
    with _wm_session_lock:
        _wm_session = None
        if _wm_session_pool_account_id is not None:
            conn = db.get_connection()
            try:
                db.release_pool_account(conn, _wm_session_pool_account_id)
            finally:
                conn.close()
            _wm_session_pool_account_id = None


def _run_wordmark_search_job(search_id: int, text: str, option: str, class_code: int) -> None:
    """Background body of /api/scrape-wordmark-class — reuses the shared
    tmrpublicsearch session (see _wm_get_or_login_session) so only the
    first search after a restart/session-death pays the cold-login cost.

    Progress lives in the DB (wordmark_searches.result_count +
    wordmark_search_results rows), not just in-memory, via on_batch pushing
    each parsed chunk to the DB as soon as it's ready — a client polling
    GET /api/scrape-wordmark-class/<search_id> sees the result count (and
    can fetch the new rows) grow live through a search that takes a while,
    instead of a long blank wait followed by everything appearing at once.
    _wordmark_jobs (in-memory) only carries the short-lived "step" text
    (connecting/logging in) since that's cheap, ephemeral, and not worth a
    DB write every few hundred ms.
    """
    with _wordmark_jobs_lock:
        _wordmark_jobs[search_id] = {"step": "Starting…"}

    def _set_step(step: str) -> None:
        with _wordmark_jobs_lock:
            job = _wordmark_jobs.get(search_id)
            if job is not None:
                job["step"] = step

    def _on_batch(batch: list[dict]) -> None:
        conn = db.get_connection()
        try:
            # row_order continuity: ask the DB how many rows this search
            # already has rather than tracking a counter here, so a retry/
            # resumed batch can't silently duplicate or skip row_order.
            existing = db.get_wordmark_search(conn, search_id)
            start_order = (existing["result_count"] if existing else 0)
            db.append_wordmark_results(conn, search_id, batch, start_order)
        finally:
            conn.close()

    sess, err = _wm_get_or_login_session(_set_step)
    if sess is None:
        conn = db.get_connection()
        try:
            db.finish_wordmark_search(conn, search_id, "error", err or "Could not authenticate.")
        finally:
            conn.close()
        with _wordmark_jobs_lock:
            _wordmark_jobs.pop(search_id, None)
        return

    try:
        if _wm_search_lock.locked():
            _set_step("Waiting for another search to finish…")
        with _wm_search_lock:
            _set_step(f"Searching for '{text}' (class {class_code})…")
            results = sess.search_wordmark(text, option=option, class_code=class_code, on_batch=_on_batch)
        conn = db.get_connection()
        try:
            if not results:
                # Nothing found — not worth keeping in History (see
                # user request: 0-result searches like a typo'd word
                # mark should disappear entirely, not just be hidden).
                db.delete_wordmark_search(conn, search_id)
            else:
                db.finish_wordmark_search(conn, search_id, "done")
        finally:
            conn.close()
    except Exception as exc:
        # Any failure while USING an already-cached session is treated as
        # the session having gone bad (expired tabToken, portal-side
        # logout, etc.) — drop it so the next search re-logs in fresh
        # rather than repeating the same failure forever.
        logging.exception("Wordmark+Class search failed for search %s — dropping cached session", search_id)
        _wm_release_session(died=True)
        conn = db.get_connection()
        try:
            db.finish_wordmark_search(conn, search_id, "error", str(exc))
        finally:
            conn.close()
    finally:
        with _wordmark_jobs_lock:
            _wordmark_jobs.pop(search_id, None)


@app.route("/api/scrape-wordmark-class", methods=["POST"])
def api_scrape_wordmark_class():
    """Search IP India's public Wordmark search (tmrpublicsearch portal) by
    word mark text + trademark Class — the only portal section that
    supports both together (eStatus's own name search has no Class filter).

    Body: {"text": "TATA", "option": "start_with"|"contains"|"match_with", "class_code": 1-45}
    Returns: {"search_id": ...}, 202 — poll /api/scrape-wordmark-class/<search_id>
    (search_id IS the wordmark_searches row id, also what History/Excel
    export use — there's no separate ephemeral job id for this endpoint).
    """
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    option = str(payload.get("option", "contains")).strip()
    class_code = payload.get("class_code")
    requested_by = str(payload.get("requested_by", "")).strip() or None
    if not text:
        return jsonify({"error": "text is required."}), 400
    try:
        class_code = int(class_code)
    except (TypeError, ValueError):
        class_code = 0
    if not (1 <= class_code <= 45):
        return jsonify({"error": "class_code must be between 1 and 45."}), 400

    conn = db.get_connection()
    try:
        search_id = db.create_wordmark_search(conn, text, option, class_code, requested_by)
    finally:
        conn.close()

    threading.Thread(
        target=_run_wordmark_search_job,
        args=(search_id, text, option, class_code),
        daemon=True,
    ).start()
    return jsonify({"search_id": search_id}), 202


@app.route("/api/scrape-wordmark-class/<int:search_id>", methods=["GET"])
def api_scrape_wordmark_class_status(search_id):
    """Poll one search's progress. ?after=<row_order> returns only rows
    newer than that (for incremental UI rendering) instead of the whole
    result set every poll — pass the highest row_order you've already
    received (or omit/‑1 for everything so far)."""
    conn = db.get_connection()
    try:
        search = db.get_wordmark_search(conn, search_id)
        if search is None:
            return jsonify({"status": "unknown"}), 404
        after = request.args.get("after", -1, type=int)
        new_rows = db.list_wordmark_search_results(conn, search_id, after_row_order=after)
    finally:
        conn.close()

    with _wordmark_jobs_lock:
        step = (_wordmark_jobs.get(search_id) or {}).get("step", "")

    return jsonify({
        "status": search["status"],
        "step": _sanitize_status_text(step),
        "message": _sanitize_status_text(search.get("error")) or (f"Found {search['result_count']} result(s)." if search["status"] == "done" else ""),
        "result_count": search["result_count"],
        "results": new_rows,
    })


_all_classes_jobs: dict[str, dict] = {}
_all_classes_jobs_lock = threading.Lock()


def _run_all_classes_job(job_id: str, text: str, option: str, class_codes: list[int]) -> None:
    """Background body of /api/scrape-wordmark-allclasses: runs a Word Mark
    search across the given list of classes, one at a time on the shared
    tmrpublicsearch session, exactly like a single-class search does per
    class — the portal itself has no "all classes" query (it rejects
    classCode=0/blank, both client- and server-side; see
    TmrPublicSearchSession.search_wordmark's own docstring), so searching
    more than one class can only mean "run the same search once per class"
    — this just automates that loop instead of making someone click Search
    once per class by hand. class_codes can be any subset of 1-45 (not
    necessarily all of them) — "All Classes" in the UI is just this same
    endpoint called with every class 1-45.

    Reuses _run_wordmark_search_job as-is for each class (same session
    reuse, same DB writes, same per-class wordmark_searches row a normal
    single-class search would produce — so History/Excel export etc. all
    keep working unchanged) rather than a parallel results pipeline; this
    function's only job is to drive the loop and track overall progress for
    the frontend to poll as one thing instead of len(class_codes) things.
    """
    total = len(class_codes)
    with _all_classes_jobs_lock:
        _all_classes_jobs[job_id] = {
            "status": "running", "current_class": 0, "total_classes": total,
            "search_ids": [], "total_results": 0, "step": "Starting…",
        }

    def _set_step(step: str) -> None:
        with _all_classes_jobs_lock:
            job = _all_classes_jobs.get(job_id)
            if job is not None:
                job["step"] = step

    for i, class_code in enumerate(class_codes, start=1):
        with _all_classes_jobs_lock:
            job = _all_classes_jobs.get(job_id)
            if job is None:  # caller gave up / job was cleaned up
                return
            job["current_class"] = i
        _set_step(f"Class {class_code} ({i} of {total})…")

        conn = db.get_connection()
        try:
            search_id = db.create_wordmark_search(conn, text, option, class_code, "all_classes")
        finally:
            conn.close()

        with _all_classes_jobs_lock:
            _all_classes_jobs[job_id]["search_ids"].append(search_id)

        # Runs synchronously here (not its own thread) — the whole point of
        # this loop is ONE class at a time on the ONE shared session; two
        # classes running concurrently would race on the same
        # requests.Session exactly like the docstring on _wm_search_lock
        # (in app.py, guarding this exact session) warns against.
        _run_wordmark_search_job(search_id, text, option, class_code)

        conn = db.get_connection()
        try:
            finished = db.get_wordmark_search(conn, search_id)
        finally:
            conn.close()
        with _all_classes_jobs_lock:
            job = _all_classes_jobs.get(job_id)
            if job is not None and finished is not None:
                job["total_results"] += finished.get("result_count") or 0

    with _all_classes_jobs_lock:
        job = _all_classes_jobs.get(job_id)
        if job is not None:
            job["status"] = "done"
            job["step"] = f"Done — {job['total_results']} result(s) across {total} class(es)."


@app.route("/api/scrape-wordmark-allclasses", methods=["POST"])
def api_scrape_wordmark_allclasses():
    """Search IP India's Wordmark portal across a chosen set of classes for
    one word mark text, one class at a time on the shared session — see
    _run_all_classes_job's docstring for why this can't be a single portal
    request. Body: {"text": "TATA", "option": "start_with"|"contains"|"match_with",
    "class_codes": [1,2,3] | omitted-or-empty for every class 1-45}
    Returns: {"job_id": ...}, 202 — poll /api/scrape-wordmark-allclasses/<job_id>."""
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    option = str(payload.get("option", "contains")).strip()
    if not text:
        return jsonify({"error": "text is required."}), 400

    raw_codes = payload.get("class_codes")
    if raw_codes:
        try:
            class_codes = sorted({int(c) for c in raw_codes if 1 <= int(c) <= 45})
        except (TypeError, ValueError):
            return jsonify({"error": "class_codes must be a list of integers 1-45."}), 400
        if not class_codes:
            return jsonify({"error": "class_codes had no valid class numbers (1-45)."}), 400
    else:
        class_codes = list(range(1, 46))  # omitted/empty = every class

    job_id = uuid.uuid4().hex
    threading.Thread(
        target=_run_all_classes_job,
        args=(job_id, text, option, class_codes),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id}), 202


@app.route("/api/scrape-wordmark-allclasses/<job_id>", methods=["GET"])
def api_scrape_wordmark_allclasses_status(job_id):
    """Poll overall progress across the per-class searches. Each individual
    class's own rows/results are read the normal way, via
    /api/scrape-wordmark-class/<search_id> for each id in search_ids —
    this endpoint only reports which class is currently running and the
    running total, not the actual result rows (avoids re-sending every
    selected class's worth of rows on every poll)."""
    with _all_classes_jobs_lock:
        job = _all_classes_jobs.get(job_id)
        if job is None:
            return jsonify({"status": "unknown"}), 404
        out = dict(job)
    out["step"] = _sanitize_status_text(out.get("step"))
    return jsonify(out)


@app.route("/api/wordmark-search-history", methods=["GET"])
def api_wordmark_search_history():
    """Past Word Mark + Class searches, newest first — ?limit=N, default 30."""
    limit = request.args.get("limit", 30, type=int)
    conn = db.get_connection()
    try:
        rows = db.list_wordmark_searches(conn, limit)
    finally:
        conn.close()
    return jsonify({"history": rows})


_WORDMARK_EXPORT_DIR = DATA_DIR / "wordmark_exports"
_TM_LOGO_PATH = (
    Path(__file__).parent / "tm_manager" / "frontend" / "public" / "apple-touch-icon.png"
)


def _draw_watermark_and_footer(canvas, doc) -> None:
    """One large, centered, semi-transparent 'MyTmTracker' watermark behind
    the page content (not a repeated small-text tile — that read as messy
    clutter, see the live screenshot this replaced), plus a page-number
    footer. Drawn via onPage callbacks so it's independent of the table
    content and applies uniformly to every page."""
    from reportlab.lib.pagesizes import A4

    page_w, page_h = A4
    canvas.saveState()
    canvas.setFont("Helvetica-Bold", 60)
    canvas.setFillColorRGB(0.15, 0.14, 0.29, alpha=0.16)
    canvas.translate(page_w / 2, page_h / 2)
    canvas.rotate(40)
    canvas.drawCentredString(0, 0, "MyTmTracker")
    canvas.restoreState()

    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor("#5A6683")
    canvas.drawCentredString(page_w / 2, 10 * 2.834645669, f"Page {doc.page}  ·  Generated by MyTmTracker")
    canvas.restoreState()


def _run_wordmark_pdf_job(pdf_job_id: str, search_id: int) -> None:
    """Background body of the PDF export — builds a watermarked, branded
    results table PDF from a search's ALREADY-SAVED rows (no re-scrape) so
    exporting a 5000+ row search doesn't block the request or pay another
    live-search cost. reportlab's Table flowable paginates automatically
    across as many pages as the row count needs."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Image, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    conn = db.get_connection()
    try:
        search = db.get_wordmark_search(conn, search_id)
        if search is None:
            db.finish_wordmark_excel_job(conn, pdf_job_id, "error", error="Search not found.")
            return
        rows = db.list_wordmark_search_results(conn, search_id, after_row_order=-1, limit=None)
    finally:
        conn.close()

    try:
        _WORDMARK_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        file_path = _WORDMARK_EXPORT_DIR / f"{pdf_job_id}.pdf"

        doc = SimpleDocTemplate(
            str(file_path), pagesize=A4,
            leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        )
        styles = getSampleStyleSheet()
        cell_style = ParagraphStyle("cell", parent=styles["Normal"], fontSize=6.6, leading=8)
        title_style = ParagraphStyle("title", parent=styles["Heading2"], fontSize=14, textColor=colors.HexColor("#152449"))
        sub_style = ParagraphStyle("sub", parent=styles["Normal"], fontSize=8.5, textColor=colors.HexColor("#5A6683"), spaceAfter=10)

        header = ["App No.", "Trade Mark", "Class", "App. Date", "Proprietor", "Used Since", "Valid Upto", "Status", "Description"]
        # Fitted to A4 PORTRAIT usable width (~182mm after 14mm margins)
        # instead of the previous landscape layout.
        col_widths = [16 * mm, 22 * mm, 10 * mm, 16 * mm, 30 * mm, 16 * mm, 16 * mm, 20 * mm, 36 * mm]
        table_style = TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#152449")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 7),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D3DAE8")),
            # No alternating row background: an opaque cell color would
            # completely hide the watermark drawn on the canvas underneath
            # it (row shading paints over everything below it regardless of
            # the watermark's own opacity) — grid lines alone still make
            # the table easy to scan, and the watermark is the point here.
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ])

        # Branded header: logo + title/subtitle side by side, using a
        # borderless 2-col table so the image and text align on one line
        # regardless of title length.
        story = []
        title_block = [
            Paragraph(f"Word Mark Search — {search['search_text']}", title_style),
            Paragraph(
                f"{search['search_option'].replace('_', ' ').title()} · Class {search['class_code']} · "
                f"{len(rows)} result(s) · {search['created_at']:%d %b %Y}",
                sub_style,
            ),
        ]
        if _TM_LOGO_PATH.is_file():
            logo = Image(str(_TM_LOGO_PATH), width=13 * mm, height=13 * mm)
            header_table = Table([[logo, title_block]], colWidths=[16 * mm, None])
            header_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.append(header_table)
        else:
            story.extend(title_block)
        story.append(Spacer(1, 10))

        # One giant Table flowable for a 200+ row search doesn't split
        # cleanly across pages (reportlab raised "too large on page N" —
        # a single Table's internal row-splitting has its own limits, hit
        # well before an A4 frame's actual row capacity). Chunking into
        # page-sized tables (each with its own header via repeatRows)
        # sidesteps that entirely — SimpleDocTemplate just flows each
        # chunk onto a new page as needed, same as normal document flow.
        _ROWS_PER_CHUNK = 26

        def _cell(value, max_chars: int = 300) -> Paragraph:
            # Some Class Description values are genuine multi-thousand-
            # character legal boilerplate (seen live: an 888pt-tall single
            # cell that blew a whole Table flowable's page-fit check) — cap
            # any one cell's text so no row can ever balloon past a
            # reasonable height. This is a display cap for the PDF table
            # only; the full text is still on file in the database.
            text = str(value or "").replace("\r\n", " ").replace("\n", " ")
            if len(text) > max_chars:
                text = text[:max_chars].rstrip() + "…"
            return Paragraph(text, cell_style)

        # ONE table for the whole result set (not one per chunk) — the
        # earlier "too large" crash was actually caused by a single
        # abnormally tall row (an 888pt cell from multi-thousand-character
        # Class Description text), not by total table size; now that cell
        # text is capped (_cell below), reportlab's own Table.split() paginates
        # a single table cleanly, repeating the header row ONLY at real page
        # breaks. Manual per-chunk tables caused a second header to appear
        # mid-page whenever a chunk boundary didn't line up with the actual
        # page edge — a real bug seen live 2026-09-16.
        if rows:
            table_data = [header]
            for r in rows:
                table_data.append([
                    _cell(r.get("app_no")),
                    _cell(r.get("trade_mark")),
                    _cell(r.get("class_number")),
                    _cell(r.get("application_date")),
                    _cell(r.get("proprietor_name"), max_chars=90),
                    _cell(r.get("use_statement")),
                    _cell(r.get("valid_upto")),
                    _cell(r.get("status")),
                    _cell(r.get("class_description"), max_chars=140),
                ])
            full_table = Table(table_data, colWidths=col_widths, repeatRows=1)
            full_table.setStyle(table_style)
            story.append(full_table)
        else:
            story.append(Paragraph("No results.", cell_style))

        doc.build(story, onFirstPage=_draw_watermark_and_footer, onLaterPages=_draw_watermark_and_footer)

        conn = db.get_connection()
        try:
            db.finish_wordmark_excel_job(conn, pdf_job_id, "done", file_path=str(file_path))
        finally:
            conn.close()
    except Exception as exc:
        logging.exception("Wordmark PDF export failed for job %s (search %s)", pdf_job_id, search_id)
        conn = db.get_connection()
        try:
            db.finish_wordmark_excel_job(conn, pdf_job_id, "error", error=str(exc))
        finally:
            conn.close()


@app.route("/api/wordmark-search/<int:search_id>/export-pdf", methods=["POST"])
def api_wordmark_export_pdf(search_id):
    """Kick off a background watermarked-PDF export of an already-completed
    search's saved results. Returns immediately — poll
    /api/wordmark-search/export-pdf/<pdf_job_id> for readiness, then
    GET .../download to get the file. Never blocks on the export itself,
    even for a 5000+ row search."""
    conn = db.get_connection()
    try:
        search = db.get_wordmark_search(conn, search_id)
        if search is None:
            return jsonify({"error": "Search not found."}), 404
        pdf_job_id = str(uuid.uuid4())
        db.create_wordmark_excel_job(conn, pdf_job_id, search_id)
    finally:
        conn.close()

    threading.Thread(target=_run_wordmark_pdf_job, args=(pdf_job_id, search_id), daemon=True).start()
    return jsonify({"pdf_job_id": pdf_job_id}), 202


@app.route("/api/wordmark-search/export-pdf/<pdf_job_id>", methods=["GET"])
def api_wordmark_export_pdf_status(pdf_job_id):
    conn = db.get_connection()
    try:
        job = db.get_wordmark_excel_job(conn, pdf_job_id)
    finally:
        conn.close()
    if job is None:
        return jsonify({"status": "unknown"}), 404
    return jsonify({"status": job["status"], "error": job.get("error")})


@app.route("/api/wordmark-search/export-pdf/<pdf_job_id>/download", methods=["GET"])
def api_wordmark_export_pdf_download(pdf_job_id):
    from flask import send_file
    conn = db.get_connection()
    try:
        job = db.get_wordmark_excel_job(conn, pdf_job_id)
    finally:
        conn.close()
    if job is None or job["status"] != "done" or not job.get("file_path"):
        abort(404)
    path = Path(job["file_path"])
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"wordmark-search-{job['wordmark_search_id']}.pdf")


@app.route("/api/pool/accounts", methods=["GET"])
def api_pool_accounts():
    conn = db.get_connection()
    try:
        grouped = db.list_pool_accounts_by_role(conn)
    finally:
        conn.close()

    def _clean(rows):
        # never leak app_password over the API
        return [
            {k: v for k, v in r.items() if k != "app_password"}
            for r in rows
        ]

    return jsonify({
        "warm": _clean(grouped["warm"]),
        "wordmark": _clean(grouped["wordmark"]),
        "cold": _clean(grouped["cold"]),
        "warm_pool": warm_pool.health(),
        "warm_slots": warm_pool.status_snapshot(),
    })


@app.route("/api/pool/accounts/<path:email>/role", methods=["POST"])
def api_pool_account_role(email):
    payload = request.get_json(silent=True) or {}
    role = str(payload.get("role", "")).strip().lower()
    if role not in ("warm", "wordmark", "cold"):
        return jsonify({"error": "role must be 'warm', 'wordmark', or 'cold'"}), 400
    conn = db.get_connection()
    try:
        row = db.set_pool_account_role(conn, email, role)
    finally:
        conn.close()
    if row is None:
        return jsonify({"error": f"no pool account {email!r}"}), 404
    # apply live without a restart — only 'warm' touches warm_pool.py's
    # permanently-logged-in slots; 'wordmark' just reserves the account for
    # _wm_get_or_login_session's claim above; moving OFF warm still needs
    # remove_slot regardless of which role it's moving to.
    try:
        if role == "warm":
            warm_pool.add_slot(row)
        else:
            warm_pool.remove_slot(email)
    except Exception:
        logging.exception("warm_pool live role-change failed for %s", email)
    return jsonify({"ok": True, "email": email, "role": role})


@app.route("/ipindia/appno/<app_no>", methods=["GET"])
def ipindia_result_by_appno(app_no):
    """Open the result/detail page for an application number even when the
    caller has no ipindia_results.id to hand — used by the agent-history
    cards, whose rows come from tma_documents and may not carry a search_id.

    Finds the newest ipindia_results row for this app_no; if none exists but
    we do have a tma_documents extract (e.g. imported from disk), synthesizes
    a minimal 'appno' result row from it so the detail template has something
    to render. 404s only when there's genuinely nothing on file.
    """
    from psycopg.types.json import Jsonb

    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT id FROM ipindia_results
            WHERE query = %s AND search_type IN ('appno', 'appno_discover', 'appno_batch')
            ORDER BY (error = '' OR error IS NULL) DESC, scraped_at DESC
            LIMIT 1
            """,
            (app_no,),
        ).fetchone()
        if row:
            search_id = row["id"]
        else:
            tma = db.get_tma_document(conn, app_no)
            if not tma:
                abort(404)
            payload = {
                "Trade Mark": tma.get("mark_trade_mark"),
                "Application Number": app_no,
                "Class": str(tma["class_number"]) if tma.get("class_number") else None,
                "Status": None,
                "Date of Application": tma.get("application_date"),
                "Proprietor Name": tma.get("applicant_name"),
                "_source": "tma-documents",
            }
            search_id = conn.execute(
                """
                INSERT INTO ipindia_results (query, search_type, email_used, data_json, error)
                VALUES (%s, 'appno', 'derived-from-tma', %s, '')
                RETURNING id
                """,
                (app_no, Jsonb([payload])),
            ).fetchone()["id"]
            conn.commit()
    finally:
        conn.close()

    return redirect(url_for("ipindia_result", search_id=search_id, back=request.args.get("back", "")))


@app.route("/ipindia/result/<int:search_id>", methods=["GET"])
def ipindia_result(search_id):
    import json
    conn = db.get_connection()
    search = db.get_ipindia_search(conn, search_id)
    conn.close()
    if search is None:
        abort(404)

    # data_json is a JSONB column — psycopg already deserializes it to a
    # native Python list/dict, but fall back to json.loads for the rare case
    # it's still a raw string (e.g. hand-edited data).
    raw = search["data_json"]
    if isinstance(raw, str):
        try:
            results = json.loads(raw or "[]")
        except Exception:
            results = []
    else:
        results = raw or []

    # Build column headers from all keys across all results
    all_keys: list[str] = []
    seen_keys: set[str] = set()
    for row in results:
        for k in row.keys():
            if k not in seen_keys:
                all_keys.append(k)
                seen_keys.add(k)

    # If the search session (pending_id) is still alive, offer a "search
    # another number" shortcut that skips login/OTP.
    pending_id = request.args.get("pending_id", "")
    session_alive = bool(pending_id and pending_id in _pending_ip_sessions)

    # Documents auto-saved to disk for this application number (if any),
    # plus any extracted TM-A form field data (from the tma_documents table).
    documents: list[str] = []
    tma_details: dict | None = None
    reminders: list[dict] = []
    correspondence: list[dict] = []
    if search["search_type"] in ("appno", "appno_discover"):
        conn2 = db.get_connection()
        # Reads tma_documents directly rather than globbing the local disk
        # folder — a Drive-first upload (no local file at all) would
        # otherwise never show up here, even though ipindia_document
        # (which actually serves it) already knows how to fetch it from
        # Drive.
        documents = [d["filename"] for d in db.list_tma_documents(conn2, search["query"])]
        tma_row = db.get_tma_document(conn2, search["query"])
        reminders = db.list_ipindia_reminders(conn2, search["query"])
        correspondence = db.list_ipindia_correspondence(conn2, search["query"])
        conn2.close()
        if tma_row:
            tma_details = {
                "form_type": tma_row.get("form_type", "TM-A"),
                "temp_ref_no": tma_row["temp_ref_no"],
                "fee": tma_row["fee"],
                "application_filed_as": tma_row["application_filed_as"],
                "language": tma_row["language"],
                "applicant": {
                    "name": tma_row["applicant_name"],
                    "partners": tma_row["applicant_partners"],
                    "address": tma_row["applicant_address"],
                    "address_for_service": tma_row["applicant_address_for_service"],
                    "country": tma_row["applicant_country"],
                    "jurisdiction": tma_row["applicant_jurisdiction"],
                    "mobile": tma_row["applicant_mobile"],
                    "email": tma_row["applicant_email"],
                    "nature": tma_row["applicant_nature"],
                    "legal_status": tma_row["applicant_legal_status"],
                } if (tma_row["applicant_name"] or tma_row["applicant_address"]) else None,
                "agent": {
                    "name": tma_row["agent_name"],
                    "address": tma_row["agent_address"],
                    "nature": tma_row["agent_nature"],
                    "registration_no": tma_row["agent_registration_no"],
                } if (tma_row["agent_name"] or tma_row["agent_address"]) else None,
                "mark": {
                    "category": tma_row["mark_category"],
                    "trade_mark": tma_row["mark_trade_mark"],
                    "image_description": tma_row["mark_image_description"],
                } if tma_row["mark_trade_mark"] else None,
                "class": {
                    "number": tma_row["class_number"],
                    "description": tma_row["class_description"],
                } if tma_row["class_number"] else None,
                "use_statement": tma_row["use_statement"],
                "other_information": tma_row["other_information"],
                "verification": {
                    "date": tma_row["verification_date"],
                    "signed_for": tma_row["verification_signed_for"],
                } if (tma_row["verification_date"] or tma_row["verification_signed_for"]) else None,
                "receipt_no": tma_row.get("receipt_no"),
                "receipt_date": tma_row.get("receipt_date"),
                "receipt_amount": tma_row.get("receipt_amount"),
                "form_no": tma_row.get("form_no"),
                "agent_code": tma_row.get("agent_code"),
                "proprietor_code": tma_row.get("proprietor_code"),
                "application_date": tma_row.get("application_date"),
                "registry_office": tma_row.get("registry_office"),
                "logo_filename": Path(tma_row["logo_path"]).name if tma_row.get("logo_path") else None,
            }

    # Optional "came from" link so the back-button returns to whichever
    # history view (date/range/status) the user actually clicked in from,
    # instead of always going to the search form. Only accept relative,
    # same-app paths — never an absolute/external URL.
    back_url = request.args.get("back", "")
    if not back_url.startswith("/") or back_url.startswith("//"):
        back_url = None

    return render_template(
        "ipindia_result.html",
        search=search,
        results=results,
        columns=all_keys,
        pending_id=pending_id,
        session_alive=session_alive,
        documents=documents,
        tma_details=tma_details,
        reminders=reminders,
        correspondence=correspondence,
        back_url=back_url,
    )


def _try_drive_download(file_id: str) -> bytes | None:
    """Best-effort Drive download — returns bytes on success, None on any
    failure (Drive not configured, connect failed, download failed). Never
    raises; caller falls back to local disk exactly like a Drive outage
    never being a hard failure for this app. Mirrors tm_manager/app.py's
    identical helper."""
    if not config.is_drive_configured():
        return None
    try:
        from google_drive import DriveClient
        drive = DriveClient()
        if not drive.connect():
            return None
        return drive.download_bytes(file_id)
    except Exception:
        logging.exception("Drive download failed for file_id=%s", file_id)
        return None


@app.route("/ipindia/document/<query>/<filename>", methods=["GET"])
def ipindia_document(query, filename):
    """Serve a previously auto-saved document PDF for download — Drive
    first if this row has a drive_file_id, falling back to local disk
    otherwise (query is the app_no; see the on_doc_saved callback above)."""
    conn = db.get_connection()
    try:
        doc = db.get_tma_document_by_filename(conn, query, filename)
    finally:
        conn.close()

    if doc and doc.get("drive_file_id"):
        drive_bytes = _try_drive_download(doc["drive_file_id"])
        if drive_bytes is not None:
            from flask import send_file
            from io import BytesIO
            return send_file(BytesIO(drive_bytes), mimetype="application/pdf",
                              as_attachment=True, download_name=filename)

    from flask import send_from_directory
    folder = DATA_DIR / "ipindia" / slugify(query)
    if not folder.is_dir():
        abort(404)
    return send_from_directory(folder, filename, as_attachment=True)


@app.route("/ipindia/logo/<query>/<filename>", methods=["GET"])
def ipindia_logo(query, filename):
    """
    Serve a cropped trademark logo image (extracted from a TM-A PDF) inline
    for use in <img> tags — unlike ipindia_document, this is not forced to
    download since it's meant to be displayed directly in the UI. Drive
    first if this row has a logo_drive_file_id, falling back to local disk.
    """
    conn = db.get_connection()
    try:
        drive_file_id = db.get_tma_logo_drive_id(conn, query, filename)
    finally:
        conn.close()

    if drive_file_id:
        drive_bytes = _try_drive_download(drive_file_id)
        if drive_bytes is not None:
            from flask import send_file
            from io import BytesIO
            return send_file(BytesIO(drive_bytes), mimetype="image/png", as_attachment=False)

    from flask import send_from_directory
    folder = DATA_DIR / "ipindia" / slugify(query)
    if not folder.is_dir():
        abort(404)
    return send_from_directory(folder, filename, as_attachment=False)


@app.route("/ipindia/reminder-document/<query>/<filename>", methods=["GET"])
def ipindia_reminder_document(query, filename):
    """Serve a previously auto-saved reminder document (PDF or, more often,
    the HTML notice page the portal actually renders) for viewing — Drive
    first, falling back to local disk otherwise."""
    conn = db.get_connection()
    try:
        drive_file_id = db.get_reminder_drive_id(conn, query, filename)
    finally:
        conn.close()

    if drive_file_id:
        drive_bytes = _try_drive_download(drive_file_id)
        if drive_bytes is not None:
            from flask import send_file
            from io import BytesIO
            mimetype = "text/html" if filename.endswith((".html", ".htm")) else "application/pdf"
            return send_file(BytesIO(drive_bytes), mimetype=mimetype,
                              as_attachment=filename.endswith(".pdf"), download_name=filename)

    from flask import send_from_directory
    folder = DATA_DIR / "ipindia" / slugify(query) / "reminders"
    if not folder.is_dir():
        abort(404)
    return send_from_directory(folder, filename, as_attachment=filename.endswith(".pdf"))


@app.route("/ipindia/correspondence-document/<query>/<filename>", methods=["GET"])
def ipindia_correspondence_document(query, filename):
    """Serve a previously auto-saved correspondence document (PDF or, more
    often, the HTML notice page the portal actually renders) for viewing —
    Drive first, falling back to local disk otherwise."""
    conn = db.get_connection()
    try:
        drive_file_id = db.get_correspondence_drive_id(conn, query, filename)
    finally:
        conn.close()

    if drive_file_id:
        drive_bytes = _try_drive_download(drive_file_id)
        if drive_bytes is not None:
            from flask import send_file
            from io import BytesIO
            mimetype = "text/html" if filename.endswith((".html", ".htm")) else "application/pdf"
            return send_file(BytesIO(drive_bytes), mimetype=mimetype,
                              as_attachment=filename.endswith(".pdf"), download_name=filename)

    from flask import send_from_directory
    folder = DATA_DIR / "ipindia" / slugify(query) / "correspondence"
    if not folder.is_dir():
        abort(404)
    return send_from_directory(folder, filename, as_attachment=filename.endswith(".pdf"))


# ── Auto Discover routes ──────────────────────────────────────────────────────

def _active_discover_job_ids() -> list[str]:
    """All currently-running Auto Discover job IDs, oldest first. Multiple
    jobs (each with its own email/session) can run at the same time."""
    with _discover_lock:
        return [job_id for job_id, job in _discover_jobs.items() if job["status"] == "running"]


def _finished_discover_job_ids(limit: int = 5) -> list[str]:
    """Most recently finished (non-running) jobs, newest first, for
    re-showing download links after a page reload."""
    with _discover_lock:
        finished = [job_id for job_id, job in _discover_jobs.items() if job["status"] != "running"]
    return list(reversed(finished))[:limit]


def _pool_counts() -> dict:
    """Lightweight pool_free_count/pool_total_count for routes that need the
    auth-mode toggle's default (pool vs manual) but don't need full series
    data — single search and Cluster Expand, unlike Auto Discover's pages
    which already get this via _series_progress_context()."""
    conn = db.get_connection()
    try:
        pool_accounts = db.list_pool_accounts(conn)
    finally:
        conn.close()
    return {
        "pool_total_count": len(pool_accounts),
        "pool_free_count": sum(1 for a in pool_accounts if a["enabled"] and not a["claimed_by_job_id"]),
    }


def _series_progress_context() -> dict:
    """series_progress + a ready-to-prefill comma-joined string of the
    current active queue — shared by every route that re-renders
    auto_discover.html so the queue editor/status table are always present.

    Each row's real_hits_count is the actual number of unique application
    numbers with a saved hit in that series' range (db.count_ipindia_hits_in_range)
    — NOT the same as hits_count, which only reflects what one tracked
    series job has personally found since its own checkpoint and understates
    reality once other scraping (earlier random-mode runs, manual searches)
    has already covered numbers ahead of that checkpoint."""
    conn = db.get_connection()
    try:
        progress = db.list_series_progress(conn)
        for r in progress:
            r["real_hits_count"] = db.count_ipindia_hits_in_range(conn, r["range_start"], r["range_end"])
        pool_accounts = db.list_pool_accounts(conn)
    finally:
        conn.close()
    active_queue = [r["series"] for r in progress if r["queue_position"] is not None]
    last_email = next((r["email_used"] for r in progress if r.get("email_used")), "")
    pool_free_count = sum(1 for a in pool_accounts if a["enabled"] and not a["claimed_by_job_id"])
    return {
        "series_progress": progress,
        "series_queue_text": ", ".join(active_queue),
        "series_last_email": last_email or "",
        "pool_total_count": len(pool_accounts),
        "pool_free_count": pool_free_count,
    }


@app.route("/discover", methods=["GET"])
def discover_index():
    return render_template(
        "auto_discover.html",
        otp_configured=config.is_otp_configured(),
        active_job_ids=_active_discover_job_ids(),
        finished_job_ids=_finished_discover_job_ids(),
        error=None,
        **_series_progress_context(),
    )


def _pool_claim_and_authenticate(job_id: str, for_batch_worker: bool = False) -> tuple["IPIndiaSession | None", dict | None, str | None]:
    """
    Claim a free email-pool account and fully log in to IP India — no human
    involved at any step. Request-context-free, so it's safe to call from an
    HTTP handler, a background thread (the auto-launcher), or any other
    caller that needs an authenticated session without a human typing an
    email or OTP.

    for_batch_worker=True: pass this from the batch auto-fetch path
    (_run_batch_job) so it never takes the last free cold account — see
    db.claim_free_pool_account for why. Every other caller (a live TM
    Search, manual entry pages) leaves this False.

    Returns (sess, pool_account, None) on success, or (None, None, error_message)
    on failure. On failure, the pool account (if one was claimed) has
    already been released — callers never need to remember to release on a
    failed login, only once their own work with a *successful* login is done
    (pool_account["id"] is what to pass to db.release_pool_account then).
    """
    blocked, remaining = is_portal_blocked()
    if blocked:
        # Checked BEFORE claiming anything — claiming a pool account only to
        # immediately fail and release it again wastes the claim itself
        # (another caller has to wait out that account's normal 120s
        # cooldown for a login that was never going to happen). See
        # ipindia_scraper.py's circuit-breaker docstring for the 2026-09-23
        # incident this exists for.
        return None, None, (
            f"IP India portal is currently blocking this machine (401 on GetCaptcha) — "
            f"pausing new login attempts for another {int(remaining // 60) + 1} minute(s)."
        )

    if local_store.LOCAL_MODE:
        # VPN is active — see local_store.py's module docstring. Claim
        # against the local SQLite mirror of otp_email_pool instead of
        # Contabo; zero chance of a 30s PoolTimeout stalling job startup.
        pool_account = local_store.claim_free_pool_account_local(job_id)
    else:
        conn = db.get_connection()
        try:
            pool_account = db.claim_free_pool_account(
                conn, job_id, for_batch_worker=for_batch_worker, machine_id=_resolve_fleet_machine_id(),
            )
        finally:
            conn.close()
    if pool_account is None:
        return None, None, "No free email pool account available right now — every account is in use or the pool is empty. Add more accounts in Settings, or switch to manual entry."

    def _release() -> None:
        if local_store.LOCAL_MODE:
            local_store.release_pool_account_local(pool_account["id"])
            return
        conn_err = db.get_connection()
        try:
            db.release_pool_account(conn_err, pool_account["id"])
        finally:
            conn_err.close()

    sess, err = authenticate_pool_account(pool_account, on_release_error=_release)
    if err:
        return None, None, err
    return sess, pool_account, None


def _release_pool_account_any_mode(pool_account_id: int) -> None:
    """Release a pool account claim on success — routes to the local
    SQLite mirror or Contabo depending on local_store.LOCAL_MODE, same
    split as _pool_claim_and_authenticate's own _release closure. Used by
    every discover/series job finally-block release (the failure path
    inside _pool_claim_and_authenticate has its own _release closure
    already) so none of those dozen call sites need to duplicate this
    branch themselves."""
    if local_store.LOCAL_MODE:
        local_store.release_pool_account_local(pool_account_id)
        return
    conn = db.get_connection()
    try:
        db.release_pool_account(conn, pool_account_id)
    finally:
        conn.close()


def _pool_login_and_launch(
    mode: str,
    target_series: str | None = None,
    target_hits: int = 0,
    delay_min: float = 2.0,
    delay_max: float = 5.0,
    range_start: int = _DISCOVER_MIN,
    range_end: int = _DISCOVER_MAX,
) -> tuple[bool, str]:
    """
    Claim+authenticate a pool account (see _pool_claim_and_authenticate) and
    launch an Auto Discover background job with it. Returns (True, job_id)
    on success or (False, error_message) on failure — request-context-free,
    so it's safe to call both from an HTTP handler (discover_start's
    auth_mode="pool" branch) and from the background auto-launcher thread
    (_auto_launch_loop), which has no request to render an error page into.
    """
    job_id = uuid.uuid4().hex
    sess, pool_account, err = _pool_claim_and_authenticate(job_id)
    if err:
        return False, err

    pending = {
        "email": pool_account["email_address"],
        "mode": mode,
        "target_series": target_series or None,
        "target_hits": target_hits,
        "delay_min": delay_min,
        "delay_max": delay_max,
        "range_start": range_start,
        "range_end": range_end,
        "pool_account_id": pool_account["id"],
        "job_id": job_id,
    }
    # _launch_discover_job() doesn't build a redirect() itself (that needs
    # an active request context, which a background thread doesn't have) —
    # it just returns the job_id, which is all this caller needs.
    _launch_discover_job(pending, sess)
    return True, job_id


@app.route("/discover/start", methods=["POST"])
def discover_start():
    """
    Step 1: validate inputs, fetch + auto-solve the login CAPTCHA, send the
    OTP request, and render an OTP-entry screen. OTP is always entered
    manually for Auto Discover (no IMAP dependency — the email used here
    doesn't have to match the configured IMAP inbox).

    mode="series" (from the "Auto Suggest / Start Next" button, or "Scan
    this series" on a series' detail page) skips the target_hits/
    start_number/end_number fields entirely — the series queue
    (ipindia_series_progress) drives what gets scanned instead.
    target_series, if given, targets exactly that series (see
    _run_series_job's only_series param) instead of pulling from the queue.

    auth_mode="pool" (vs "manual", the default/original behavior) skips the
    email field and manual OTP entry entirely: a free account is claimed
    from the OTP email pool (see db.py's pool functions), used as the login
    identity, and its OTP is auto-read via IMAP right here in this request
    — the job launches directly, no /discover/verify_otp round trip.
    """
    auth_mode = request.form.get("auth_mode", "manual").strip()
    email = request.form.get("email", "").strip()
    mode = request.form.get("mode", "single").strip()
    target_series = request.form.get("target_series", "").strip()
    delay_min_raw = request.form.get("delay_min", "").strip()
    delay_max_raw = request.form.get("delay_max", "").strip()

    errors = []
    if auth_mode != "pool" and (not email or "@" not in email):
        errors.append("Valid email address is required.")

    try:
        delay_min = float(delay_min_raw)
        delay_max = float(delay_max_raw)
        if delay_min <= 0 or delay_max <= 0 or delay_min > delay_max:
            raise ValueError
    except ValueError:
        errors.append("Delay values must be positive numbers, with min ≤ max.")
        delay_min, delay_max = 2.0, 5.0

    target_hits = 0
    range_start, range_end = _DISCOVER_MIN, _DISCOVER_MAX

    if mode == "series":
        if target_series:
            if not target_series.isdigit() or not (10 <= int(target_series) <= 99):
                errors.append("Invalid series.")
                target_series = ""
        else:
            conn = db.get_connection()
            try:
                has_claimable = any(
                    r["status"] in ("queued", "in_progress") and not r.get("claimed_by_job_id")
                    for r in db.list_series_progress(conn)
                )
            finally:
                conn.close()
            if not has_claimable:
                errors.append("Series queue is empty or every queued series is already running/done — add more series to the queue.")
    else:
        target_raw = request.form.get("target_hits", "").strip()
        start_raw = request.form.get("start_number", "").strip()
        end_raw = request.form.get("end_number", "").strip()

        try:
            target_hits = int(target_raw)
            if not (1 <= target_hits <= 500):
                raise ValueError
        except ValueError:
            errors.append("Target hit count must be a number between 1 and 500.")
            target_hits = 0

        # Optional start/end application number range. Start alone means
        # "search from here upward, never below this number" (end defaults
        # to the max 7-digit number). Both given means "random within this range."
        if start_raw:
            try:
                range_start = int(start_raw)
                if not (1 <= range_start <= 9_999_999):
                    raise ValueError
            except ValueError:
                errors.append("Start number must be a valid application number.")
        if end_raw:
            try:
                range_end = int(end_raw)
                if not (1 <= range_end <= 9_999_999):
                    raise ValueError
            except ValueError:
                errors.append("End number must be a valid application number.")
        if not errors and range_start > range_end:
            errors.append("Start number must be less than or equal to end number.")

    if errors:
        return render_template(
            "auto_discover.html",
            otp_configured=config.is_otp_configured(),
            active_job_ids=_active_discover_job_ids(),
            error=" ".join(errors),
            **_series_progress_context(),
        )

    if auth_mode == "pool":
        ok, result = _pool_login_and_launch(
            mode=mode,
            target_series=target_series or None,
            target_hits=target_hits,
            delay_min=delay_min,
            delay_max=delay_max,
            range_start=range_start,
            range_end=range_end,
        )
        if not ok:
            return render_template(
                "auto_discover.html",
                otp_configured=config.is_otp_configured(),
                active_job_ids=_active_discover_job_ids(),
                error=result,
                **_series_progress_context(),
            )
        return redirect(url_for("discover_index"))

    # Manual auth mode: fetch CAPTCHA, send OTP, then render an OTP-entry
    # screen — the human enters the code themselves via /discover/verify_otp.
    sess = IPIndiaSession(email)
    try:
        captcha_info = sess.get_captcha_for_display()
    except Exception as exc:
        logging.exception("Auto Discover CAPTCHA fetch failed")
        return render_template(
            "auto_discover.html",
            otp_configured=config.is_otp_configured(),
            active_job_ids=_active_discover_job_ids(),
            error=f"Could not fetch CAPTCHA from IP India: {exc}",
            **_series_progress_context(),
        )

    auto_answer = captcha_info.get("auto_answer")
    if auto_answer is None:
        return render_template(
            "auto_discover.html",
            otp_configured=config.is_otp_configured(),
            active_job_ids=_active_discover_job_ids(),
            error="Could not auto-solve login CAPTCHA (regex + Grok both failed). Check GROK_API_KEY in Settings.",
            **_series_progress_context(),
        )

    ok, err = sess.send_otp_only(auto_answer)
    if not ok:
        return render_template(
            "auto_discover.html",
            otp_configured=config.is_otp_configured(),
            active_job_ids=_active_discover_job_ids(),
            error=f"Could not send OTP: {err}",
            **_series_progress_context(),
        )

    pending_id = uuid.uuid4().hex
    _pending_discover[pending_id] = {
        "session": sess,
        "email": email,
        "mode": mode,
        "target_series": target_series or None,
        "target_hits": target_hits,
        "delay_min": delay_min,
        "delay_max": delay_max,
        "range_start": range_start,
        "range_end": range_end,
    }

    return render_template(
        "auto_discover.html",
        otp_configured=config.is_otp_configured(),
        active_job_ids=_active_discover_job_ids(),
        pending_id=pending_id,
        email=email,
        error=None,
        **_series_progress_context(),
    )


@app.route("/discover/verify_otp", methods=["POST"])
def discover_verify_otp():
    """Step 2: verify the manually-entered OTP, then launch the background job
    — _run_discover_job (random single-range mode) or _run_series_job
    (sequential queue-driven mode), per pending["mode"]."""
    pending_id = request.form.get("pending_id", "").strip()
    otp_code = request.form.get("otp_code", "").strip()

    pending = _pending_discover.pop(pending_id, None) if pending_id else None
    if not pending:
        return render_template(
            "auto_discover.html",
            otp_configured=config.is_otp_configured(),
            active_job_ids=_active_discover_job_ids(),
            error="Session expired. Please start over.",
            **_series_progress_context(),
        )

    sess: IPIndiaSession = pending["session"]
    ok, err = sess.verify_otp_only(otp_code)
    if not ok:
        # Put it back so the user can retry the OTP without restarting the
        # whole CAPTCHA/login flow.
        _pending_discover[pending_id] = pending
        return render_template(
            "auto_discover.html",
            otp_configured=config.is_otp_configured(),
            active_job_ids=_active_discover_job_ids(),
            pending_id=pending_id,
            email=pending["email"],
            error=f"OTP verification failed: {err}",
            **_series_progress_context(),
        )

    _launch_discover_job(pending, sess)
    return redirect(url_for("discover_index"))


def _launch_discover_job(pending: dict, sess: IPIndiaSession) -> str:
    """
    Shared job-launch logic: build the _discover_jobs entry, create its stop
    event, and dispatch the right background thread (_run_series_job or
    _run_discover_job) based on pending["mode"]. Called once the OTP has
    been verified by whichever means — manual entry (discover_verify_otp),
    automatic IMAP pool read (discover_start's pool-mode branch), or the
    background auto-launcher (_pool_login_and_launch) — since everything
    past that point is identical either way.

    Returns the job_id. Deliberately does NOT build a redirect()/url_for()
    response here — those need an active request context, which the
    auto-launcher's background thread doesn't have. HTTP callers build
    their own redirect from the returned job_id (they always redirect to
    the same place, discover_index, regardless of job_id).
    """
    mode = pending.get("mode", "single")
    job_id = pending.get("job_id") or uuid.uuid4().hex
    with _discover_lock:
        _discover_jobs[job_id] = {
            "status": "running",
            "phase": "Starting…",
            "message": "",
            "target_hits": pending["target_hits"],
            "hits": [],
            "attempts_count": 0,
            "misses_count": 0,
            "errors_count": 0,
            "email": pending["email"],
            "mode": mode,
            "current_series": None,
            "pool_account_id": pending.get("pool_account_id"),
        }
    _discover_stop_events[job_id] = threading.Event()

    if mode == "series":
        thread = threading.Thread(
            target=_run_series_job,
            args=(job_id, pending["email"], sess, pending["delay_min"], pending["delay_max"]),
            kwargs={"only_series": pending.get("target_series"), "pool_account_id": pending.get("pool_account_id")},
            daemon=True,
        )
    else:
        thread = threading.Thread(
            target=_run_discover_job,
            args=(
                job_id, pending["email"], sess, pending["target_hits"],
                pending["delay_min"], pending["delay_max"],
                pending.get("range_start", _DISCOVER_MIN), pending.get("range_end", _DISCOVER_MAX),
            ),
            kwargs={"pool_account_id": pending.get("pool_account_id")},
            daemon=True,
        )
    thread.start()

    return job_id


def _auto_launch_loop() -> None:
    """
    Background scheduler: runs forever in a daemon thread started once at
    app boot. Every tick, if there's a free pool account AND a claimable
    series in the queue AND auto-launch isn't disabled, starts one more
    series job via _pool_login_and_launch — no button click, no page load
    required. No fixed concurrency cap: capacity is simply "one job per
    currently-free, enabled pool account," so this scales automatically as
    accounts are added/removed in Settings (15 today, more later).

    One launch per tick (not a burst of every free account at once) keeps
    logins staggered rather than hitting the portal with N simultaneous
    CAPTCHA/OTP flows in the same instant — see _AUTO_LAUNCH_POLL_SECONDS.
    """
    while True:
        try:
            if not _AUTO_LAUNCH_ENABLED:
                time.sleep(_AUTO_LAUNCH_POLL_SECONDS)
                continue

            if _search_jobs_active():
                # Hold off starting a NEW series job while a live TM Search
                # is in flight — it shouldn't have to queue up behind the
                # scanner's own CAPTCHA/OTP logins on the same shared pool.
                # Jobs already running are untouched; short poll so this
                # resumes quickly once the search finishes rather than
                # waiting out the full idle-tick interval.
                time.sleep(_AUTO_LAUNCH_PAUSED_POLL_SECONDS)
                continue

            conn = db.get_connection()
            try:
                # Reap any pool claim held past all plausible job durations
                # BEFORE checking for a free account — otherwise a leaked
                # claim (thread died/hung without reaching its own release)
                # permanently shrinks the pool until this process restarts,
                # starving every caller including tm_manager's auto-fetch.
                reaped = db.release_stale_pool_claims(conn, machine_id=_resolve_fleet_machine_id())
                if reaped:
                    logging.warning("Auto-launch: reaped %d stale pool account claim(s).", reaped)
                pool_accounts = db.list_pool_accounts(conn)
                series_progress = db.list_series_progress(conn)
            finally:
                conn.close()

            has_free_account = any(a["enabled"] and not a["claimed_by_job_id"] for a in pool_accounts)
            if not has_free_account:
                time.sleep(_AUTO_LAUNCH_POLL_SECONDS)
                continue

            has_claimable_series = any(
                r["status"] in ("queued", "in_progress") and not r.get("claimed_by_job_id")
                for r in series_progress
            )
            if not has_claimable_series:
                time.sleep(_AUTO_LAUNCH_POLL_SECONDS)
                continue

            ok, result = _pool_login_and_launch(mode="series")
            if ok:
                logging.info("Auto-launch: started series job %s", result)
                time.sleep(_AUTO_LAUNCH_POLL_SECONDS)
            else:
                logging.warning("Auto-launch: could not start a job (%s) — backing off %ss", result, _AUTO_LAUNCH_RETRY_BACKOFF_SECONDS)
                time.sleep(_AUTO_LAUNCH_RETRY_BACKOFF_SECONDS)
        except Exception:
            logging.exception("Auto-launch loop tick failed — will retry")
            time.sleep(_AUTO_LAUNCH_RETRY_BACKOFF_SECONDS)


@app.route("/discover/active", methods=["GET"])
def discover_active():
    """
    Lightweight JSON list of currently-running job IDs (+ minimal summary),
    polled by auto_discover.html to pick up jobs the auto-launcher started
    while the page was already open — discover_status() gives full detail
    for one known job_id, but a page has no way to learn about a job_id it
    never saw at load time without this.
    """
    with _discover_lock:
        jobs = [
            {
                "job_id": job_id,
                "mode": j.get("mode", "single"),
                "phase": j.get("phase", ""),
                "current_series": j.get("current_series"),
            }
            for job_id, j in _discover_jobs.items()
            if j.get("status") == "running"
        ]
    return jsonify({"job_ids": [j["job_id"] for j in jobs], "jobs": jobs})


@app.route("/discover/stop/<job_id>", methods=["POST"])
def discover_stop(job_id):
    event = _discover_stop_events.get(job_id)
    if event is not None:
        event.set()
    return redirect(url_for("discover_index"))


@app.route("/discover/status/<job_id>", methods=["GET"])
def discover_status(job_id):
    with _discover_lock:
        job = _discover_jobs.get(job_id)
        if job is None:
            return jsonify({"status": "unknown"}), 404
        return jsonify({
            "status": job["status"],
            "phase": job.get("phase", ""),
            "message": job.get("message", ""),
            "target_hits": job.get("target_hits", 0),
            "hits": list(job.get("hits", [])),
            "attempts_count": job.get("attempts_count", 0),
            "misses_count": job.get("misses_count", 0),
            "errors_count": job.get("errors_count", 0),
            "mode": job.get("mode", "single"),
            "current_series": job.get("current_series"),
        })


@app.route("/discover/queue/save", methods=["POST"])
def discover_queue_save():
    """Save the user-defined series queue (comma-separated 2-digit prefixes,
    e.g. "78, 77, 65") — replaces the active queue ordering without
    resetting progress on any series already in it (see
    db.save_series_queue)."""
    raw = request.form.get("series_list", "").strip()
    tokens = [t.strip() for t in raw.split(",") if t.strip()]

    errors = []
    ordered: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if not tok.isdigit() or not (10 <= int(tok) <= 99):
            errors.append(f'"{tok}" is not a valid series — use two-digit numbers 10-99.')
            continue
        series = f"{int(tok):02d}"
        if series not in seen:
            seen.add(series)
            ordered.append(series)

    if errors:
        return render_template(
            "auto_discover.html",
            otp_configured=config.is_otp_configured(),
            active_job_ids=_active_discover_job_ids(),
            error=" ".join(errors),
            **_series_progress_context(),
        )

    conn = db.get_connection()
    try:
        db.save_series_queue(conn, ordered)
    finally:
        conn.close()

    return redirect(url_for("discover_index"))


@app.route("/discover/series/<series>", methods=["GET"])
def discover_series_detail(series):
    """
    Series detail page: progress summary, the missing (not-yet-checked)
    range still ahead of the checkpoint, every hit already found within
    this series (paginated 40/page, one card per unique application
    number — reuses existing ipindia_results data, no separate per-number
    log), and a "Scan this series" button that starts a targeted job for
    just this series (see _run_series_job's only_series param).
    """
    series = series.strip()
    if not series.isdigit() or not (10 <= int(series) <= 99):
        abort(404)
    series = f"{int(series):02d}"
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 40

    conn = db.get_connection()
    try:
        progress = db.get_series_progress(conn, series)
        all_hits = db.list_ipindia_hits_in_range(conn, progress["range_start"], progress["range_end"])
    finally:
        conn.close()

    total_hits = len(all_hits)
    total_pages = max(1, (total_hits + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    hits = all_hits[start:start + per_page]
    for h in hits:
        h["logo_filename"] = Path(h["logo_path"]).name if h.get("logo_path") else None

    checked_through = progress["last_checked_number"]
    if checked_through and checked_through >= progress["range_start"]:
        missing_start = checked_through + 1
    else:
        missing_start = progress["range_start"]
    missing_start = min(missing_start, progress["range_end"] + 1)
    missing_count = max(0, progress["range_end"] - missing_start + 1)

    return render_template(
        "discover_series_detail.html",
        series=series,
        progress=progress,
        hits=hits,
        total_hits=total_hits,
        page=page,
        total_pages=total_pages,
        missing_start=missing_start,
        missing_count=missing_count,
        active_job_ids=_active_discover_job_ids(),
    )


@app.route("/discover/series/<series>/appno/<app_no>", methods=["GET"])
def discover_appno_fetches(series, app_no):
    """
    Every time application number *app_no* has ever been scraped (newest
    first) — linked from a "N fetches" badge on a series detail card when
    the same number was found more than once (e.g. re-discovered by
    separate Auto Discover runs).
    """
    series = series.strip()
    if not series.isdigit() or not (10 <= int(series) <= 99):
        abort(404)
    series = f"{int(series):02d}"
    if not app_no.isdigit():
        abort(404)

    conn = db.get_connection()
    try:
        fetches = db.list_ipindia_fetches_for_app_no(conn, app_no)
    finally:
        conn.close()

    if not fetches:
        abort(404)

    return render_template(
        "discover_appno_fetches.html",
        series=series,
        app_no=app_no,
        fetches=fetches,
    )


@app.route("/discover/download/<job_id>", methods=["GET"])
def discover_download(job_id):
    """Download all hits found in one Auto Discover run as a CSV file."""
    import csv
    import io
    import json

    with _discover_lock:
        job = _discover_jobs.get(job_id)
        if job is None:
            abort(404)
        hits = list(job.get("hits", []))

    search_ids = [h["search_id"] for h in hits]
    conn = db.get_connection()
    searches = db.get_ipindia_searches_by_ids(conn, search_ids)
    conn.close()

    # Parse each search's data_json (a 1-item list per Auto Discover hit)
    # and flatten to rows, collecting the union of all field names for
    # consistent CSV columns. data_json is JSONB — psycopg already
    # deserializes it, but tolerate a raw string for safety.
    rows: list[dict] = []
    all_keys: list[str] = []
    seen_keys: set[str] = set()
    for search in searches:
        raw = search["data_json"]
        if isinstance(raw, str):
            try:
                results = json.loads(raw or "[]")
            except Exception:
                results = []
        else:
            results = raw or []
        for result in results:
            rows.append(result)
            for k in result.keys():
                if k not in seen_keys:
                    all_keys.append(k)
                    seen_keys.add(k)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_keys, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)

    filename = f"auto_discover_{job_id[:8]}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Cluster Expand routes ─────────────────────────────────────────────────────

def _active_cluster_job_ids() -> list[str]:
    with _cluster_lock:
        return [job_id for job_id, job in _cluster_jobs.items() if job["status"] == "running"]


@app.route("/cluster", methods=["GET"])
def cluster_index():
    """Standalone Cluster Expand page — start a crawl from any brand name
    directly, without needing to open a specific IP India result first."""
    return render_template(
        "cluster_expand.html",
        active_job_ids=_active_cluster_job_ids(),
        **_pool_counts(),
    )


def _launch_cluster_job(
    email: str,
    mark_name: str,
    sess: "IPIndiaSession",
    delay_min: float,
    delay_max: float,
    pool_account_id: int | None = None,
) -> str:
    """Shared job-launch logic for Cluster Expand, used by both the manual
    OTP-entry path (cluster_verify_otp) and the pool auto-login path
    (cluster_start's pool branch) — identical once a session is authenticated."""
    job_id = uuid.uuid4().hex
    with _cluster_lock:
        _cluster_jobs[job_id] = {
            "status": "running",
            "phase": "Starting…",
            "message": "",
            "mark_name": mark_name,
            "total_candidates": 0,
            "skipped_existing": 0,
            "hits": [],
            "attempts_count": 0,
            "misses_count": 0,
            "errors_count": 0,
            "email": email,
        }
    _cluster_stop_events[job_id] = threading.Event()

    thread = threading.Thread(
        target=_run_cluster_job,
        args=(job_id, email, sess, mark_name, delay_min, delay_max),
        kwargs={"pool_account_id": pool_account_id},
        daemon=True,
    )
    thread.start()
    return job_id


@app.route("/ipindia/cluster/start", methods=["POST"])
def cluster_start():
    """
    Step 1. Two paths:

    auth_mode="pool" (default when a free pool account exists): claim +
    fully auto-authenticate (no human involved), then launch the crawl
    directly, no OTP-entry modal — mirrors discover_start's pool branch.

    auth_mode="manual" (or no free pool account): original flow — fetch +
    auto-solve the login CAPTCHA, send the OTP request, and render an
    OTP-entry screen. Mirrors /discover/start.
    """
    auth_mode = request.form.get("auth_mode", "pool").strip()
    email = request.form.get("email", "").strip()
    mark_name = request.form.get("mark_name", "").strip()
    back_url = request.form.get("back", "").strip()
    delay_min_raw = request.form.get("delay_min", "").strip()
    delay_max_raw = request.form.get("delay_max", "").strip()

    errors = []
    if not mark_name:
        errors.append("A trade mark name to start from is required.")

    try:
        delay_min = float(delay_min_raw) if delay_min_raw else 2.0
        delay_max = float(delay_max_raw) if delay_max_raw else 5.0
        if delay_min <= 0 or delay_max <= 0 or delay_min > delay_max:
            raise ValueError
    except ValueError:
        errors.append("Delay values must be positive numbers, with min ≤ max.")
        delay_min, delay_max = 2.0, 5.0

    if not back_url.startswith("/") or back_url.startswith("//"):
        back_url = None

    if auth_mode != "pool" and (not email or "@" not in email):
        errors.append("Valid email address is required.")

    if errors:
        return render_template(
            "ipindia_cluster_modal.html",
            error=" ".join(errors), back_url=back_url,
            **_pool_counts(),
        )

    if auth_mode == "pool":
        job_id_for_claim = uuid.uuid4().hex
        sess, pool_account, err = _pool_claim_and_authenticate(job_id_for_claim)
        if err:
            return render_template(
                "ipindia_cluster_modal.html",
                error=err, back_url=back_url,
                **_pool_counts(),
            )
        job_id = _launch_cluster_job(
            pool_account["email_address"], mark_name, sess, delay_min, delay_max,
            pool_account_id=pool_account["id"],
        )
        return render_template("ipindia_cluster_job.html", job_id=job_id, back_url=back_url)

    sess = IPIndiaSession(email)
    try:
        captcha_info = sess.get_captcha_for_display()
    except Exception as exc:
        logging.exception("Cluster Expand CAPTCHA fetch failed")
        return render_template(
            "ipindia_cluster_modal.html",
            error=f"Could not fetch CAPTCHA from IP India: {exc}", back_url=back_url,
            **_pool_counts(),
        )

    auto_answer = captcha_info.get("auto_answer")
    if auto_answer is None:
        return render_template(
            "ipindia_cluster_modal.html",
            error="Could not auto-solve login CAPTCHA (regex + Grok both failed). Check GROK_API_KEY in Settings.",
            back_url=back_url,
            **_pool_counts(),
        )

    ok, err = sess.send_otp_only(auto_answer)
    if not ok:
        return render_template(
            "ipindia_cluster_modal.html",
            error=f"Could not send OTP: {err}", back_url=back_url,
            **_pool_counts(),
        )

    pending_id = uuid.uuid4().hex
    _pending_cluster[pending_id] = {
        "session": sess,
        "email": email,
        "mark_name": mark_name,
        "delay_min": delay_min,
        "delay_max": delay_max,
    }

    return render_template(
        "ipindia_cluster_modal.html",
        pending_id=pending_id, email=email, mark_name=mark_name, back_url=back_url,
    )


@app.route("/ipindia/cluster/verify_otp", methods=["POST"])
def cluster_verify_otp():
    """Step 2: verify the manually-entered OTP, then launch the background job."""
    pending_id = request.form.get("pending_id", "").strip()
    otp_code = request.form.get("otp_code", "").strip()
    back_url = request.form.get("back", "").strip()
    if not back_url.startswith("/") or back_url.startswith("//"):
        back_url = None

    pending = _pending_cluster.pop(pending_id, None) if pending_id else None
    if not pending:
        return render_template(
            "ipindia_cluster_modal.html",
            error="Session expired. Please start over.", back_url=back_url,
            **_pool_counts(),
        )

    sess: IPIndiaSession = pending["session"]
    ok, err = sess.verify_otp_only(otp_code)
    if not ok:
        _pending_cluster[pending_id] = pending
        return render_template(
            "ipindia_cluster_modal.html",
            pending_id=pending_id, email=pending["email"], mark_name=pending["mark_name"],
            error=f"OTP verification failed: {err}", back_url=back_url,
        )

    job_id = _launch_cluster_job(pending["email"], pending["mark_name"], sess, pending["delay_min"], pending["delay_max"])
    return render_template("ipindia_cluster_job.html", job_id=job_id, back_url=back_url)


@app.route("/ipindia/cluster/stop/<job_id>", methods=["POST"])
def cluster_stop(job_id):
    event = _cluster_stop_events.get(job_id)
    if event is not None:
        event.set()
    return ("", 204)


@app.route("/ipindia/cluster/status/<job_id>", methods=["GET"])
def cluster_status(job_id):
    with _cluster_lock:
        job = _cluster_jobs.get(job_id)
        if job is None:
            return jsonify({"status": "unknown"}), 404
        return jsonify({
            "status": job["status"],
            "phase": job.get("phase", ""),
            "message": job.get("message", ""),
            "mark_name": job.get("mark_name", ""),
            "total_candidates": job.get("total_candidates", 0),
            "skipped_existing": job.get("skipped_existing", 0),
            "hits": list(job.get("hits", [])),
            "attempts_count": job.get("attempts_count", 0),
            "misses_count": job.get("misses_count", 0),
            "errors_count": job.get("errors_count", 0),
        })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _open_in_explorer(folder: Path) -> None:
    if not folder.is_dir():
        return
    if sys.platform == "win32":
        os.startfile(folder)
    elif sys.platform == "darwin":
        subprocess.run(["open", str(folder)])
    else:
        subprocess.run(["xdg-open", str(folder)])


if __name__ == "__main__":
    # Release any series claims left over from the previous process run —
    # in-memory job tracking (_discover_jobs) never survives a restart, so
    # a claimed_by_job_id at startup is always orphaned and would otherwise
    # block that series from ever being resumed.
    #
    # Wrapped in try/except (new, for VPN-rotation testing): if a VPN
    # tunnel is active and Postgres is unreachable at boot, this used to
    # crash the whole process before it could even serve a single request.
    # Skipping it just means orphaned claims from a previous run get
    # cleaned up on the NEXT successful startup instead of this one —
    # exactly the same one-run delay this cleanup already tolerates for
    # fleet claims via release_stale_pool_claims's time-based reaping.
    try:
        _startup_conn = db.get_connection()
        try:
            _cleared = db.release_all_series_claims(_startup_conn)
            if _cleared:
                logging.info("Released %d orphaned series claim(s) from a previous run.", _cleared)
            _pool_cleared = db.release_all_pool_claims(_startup_conn, machine_id=_resolve_fleet_machine_id())
            if _pool_cleared:
                logging.info("Released %d orphaned pool account claim(s) from a previous run.", _pool_cleared)
            _reaped = db.reap_stale_scrape_jobs(_startup_conn)
            if _reaped:
                logging.info("Marked %d stale scrape job(s) as lost (scraper restarted mid-run).", _reaped)
        finally:
            _startup_conn.close()
    except Exception:
        logging.exception(
            "Startup DB cleanup skipped — Postgres unreachable (VPN active?). "
            "Will retry on next restart; continuing to start the app now."
        )

    # Warm session pool: starts logging in its reserved accounts (see
    # db.list_warm_pool_accounts) in the background now, so they're likely
    # ready by the time the first search request arrives. Reserved accounts
    # are excluded from claim_free_pool_account's query, so this can't race
    # the auto-launch loop below for the same accounts.
    warm_pool.start()

    # Zero-click background scanning: starts after the orphaned-claim
    # cleanup above so it never races to claim something the cleanup
    # hasn't released yet. Runs for the lifetime of the process.
    threading.Thread(target=_auto_launch_loop, daemon=True).start()
    logging.info("Auto-launch loop started (enabled=%s, poll=%ss).", _AUTO_LAUNCH_ENABLED, _AUTO_LAUNCH_POLL_SECONDS)

    # Retries any tma_documents saves that succeeded locally (PDF + extracted
    # fields on disk) but couldn't reach Contabo's database at the time —
    # e.g. this machine's VPN was on. See pending_sync.py.
    import pending_sync
    pending_sync.start_sync_loop()

    # Retries queued Auto Discover / series-scan hits whose search row
    # itself couldn't be created at all (Postgres unreachable from the very
    # start of the hit, not just the one document-save step above) — see
    # offline_queue.py.
    offline_queue.start_sync_loop()

    # VPN rotation: usage tracking + the auto-switch-on-block hook (see
    # ipindia_scraper.record_portal_401) are armed whenever vpn-configs/
    # has any .conf files, but this process does NOT auto-connect a tunnel
    # at startup — that only happens on a real IP India block (see
    # ipindia_scraper.record_portal_401, which POSTs to /api/vpn/connect)
    # or manually via that same route. Once connected, the scraping path
    # (Auto Discover / series jobs) runs fully local-only (local_store.py)
    # so it never depends on Postgres being reachable through the tunnel —
    # but other app routes (homepage, result pages) still need Contabo
    # directly and WILL error while VPN is on; that's accepted, not fixed.
    # No-op (and harmless) on any machine without a vpn-configs folder,
    # e.g. Contabo itself.
    import vpn_rotator
    if vpn_rotator.list_servers():
        vpn_rotator.start_usage_tracker()
        vpn_rotator.start_recheck_loop()

    # Optional HTTP Basic auth gate — set BASIC_AUTH_USER / BASIC_AUTH_PASS in
    # .env to require credentials on every request (the code comment below is
    # right: this app has no real auth and stores credentials in .env).
    # EXCEPT: the public search endpoints the TM Manager frontend calls
    # directly on behalf of real logged-in clients (via /scraper-api) —
    # those clients authenticate against TM Manager's own login, not this
    # ops-only credential, so gating them here just breaks their search with
    # a browser Basic-Auth popup they have no way to answer. Every other
    # route on this service (admin/ops tooling) keeps requiring Basic-Auth
    # exactly as before.
    _PUBLIC_SEARCH_PATH_RE = re.compile(
        r"^/api/(scrape-appno|scrape-status(/.*)?|scrape-wordmark-class(/.*)?"
        r"|scrape-wordmark-allclasses(/.*)?|wordmark-search-history"
        r"|wordmark-search/.*)$"
    )

    if config.BASIC_AUTH_USER and config.BASIC_AUTH_PASS:
        import base64 as _b64

        @app.before_request
        def _require_basic_auth():
            if _PUBLIC_SEARCH_PATH_RE.match(request.path):
                return None
            hdr = request.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    u, _, p = _b64.b64decode(hdr[6:]).decode("utf-8", "replace").partition(":")
                    if u == config.BASIC_AUTH_USER and p == config.BASIC_AUTH_PASS:
                        return None
                except Exception:
                    pass
            return ("Auth required", 401, {"WWW-Authenticate": 'Basic realm="datascraper"'})

    # NOTE: bound to 0.0.0.0 — reachable from other devices on the local
    # network. Without Basic auth (above) this app has no authentication —
    # only expose it on a trusted private network.
    if config.USE_WAITRESS:
        from waitress import serve
        logging.info("Serving with waitress on 0.0.0.0:5001")
        serve(app, host="0.0.0.0", port=5001, threads=8)
    else:
        # threaded=True: serve tm_manager's /api/scrape-appno concurrently
        # with browser traffic instead of one-request-at-a-time.
        app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
