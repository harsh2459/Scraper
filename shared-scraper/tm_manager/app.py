"""
TM Manager — a client-facing app for tracking trademark applications.
Application data is fetched and kept up to date automatically in the
background; nothing about how that happens is exposed in this app's UI.

Separate Flask process, separate codebase, same Postgres database as the
internal fetching backend. Talks to it only over a plain HTTP API — never
imports its code directly.
"""

import datetime
import hashlib
import hmac
import logging
import os
import re
import secrets
import sys
import threading
from io import BytesIO
from pathlib import Path

import numpy as np
import pdfplumber
import requests
import sentry_sdk
from flask import Flask, Response, abort, g, jsonify, redirect, request, send_file, send_from_directory, session, url_for
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from PIL import Image
from sentry_sdk.integrations.flask import FlaskIntegration
from werkzeug.security import check_password_hash, generate_password_hash

# Shared modules (fleet_storage, compliance_facts, ...) live one level up.
# Appended, not inserted, so this package's own config/db are never shadowed.
# Must precede the fleet_* imports below: run as a script, sys.path[0] is
# tm_manager/ only, so they fail in the container otherwise.
sys.path.append(str(Path(__file__).resolve().parent.parent))

import billing
import config
import db
import fleet_scheduler
import fleet_documents
import scheduler
from fleet_api import bp as fleet_bp
from tm_leads_routes import bp as tm_leads_bp
from control_admin_proxy import bp as control_admin_bp

# Sentry must be initialized before the Flask app is created so its
# FlaskIntegration can instrument request handling from the start. No-op
# (empty dsn) if SENTRY_DSN_BACKEND isn't set — safe for local dev.
sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN_BACKEND", ""),
    integrations=[FlaskIntegration()],
    traces_sample_rate=0.1,
    send_default_pii=False,
)

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
import parse_journal  # noqa: E402

# google_drive.py is a small, dependency-free utility module (only needs
# its own `config`, no scraper-specific logic) — not the same kind of
# import the module docstring's "never imports its code directly" refers
# to (that's about not reaching into the SCRAPER'S OWN fetching/session
# code). tm_manager needs this to stream a document back from Drive the
# same way pdf_scraper's upload hook needs it to send one there.
sys.path.insert(0, str(Path(__file__).parent.parent))
from google_drive import DriveClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

app = Flask(__name__)
# Production serves frontend+API from the same origin (nginx routes /api/*
# to this process, everything else to the static build) — CORS only really
# matters for local dev, where Vite serves the frontend from a different
# port than this Flask process.
CORS(app, supports_credentials=True, origins=[
    "https://mytmtracker.com", "https://www.mytmtracker.com",
    "http://localhost:5173", "http://127.0.0.1:5005",
])
app.register_blueprint(tm_leads_bp)
app.register_blueprint(fleet_bp)
app.register_blueprint(control_admin_bp)

# Rate limiting — keyed by logged-in user (g.user_id, set below in
# _require_login) so one abusive account can't hide behind a shared IP;
# falls back to remote address before login. memory:// storage is fine for
# now since each service runs as a single Waitress process — revisit if
# this ever goes multi-process, where per-process memory would let a client
# just get a fresh limit by hitting a different worker.
limiter = Limiter(
    key_func=lambda: getattr(g, "user_id", None) or get_remote_address(),
    app=app,
    storage_uri=os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://"),
    default_limits=["200 per minute"],
)

# ── Session-cookie login ────────────────────────────────────────────────────
# A real login page (not the browser's Basic-auth popup). Set BASIC_AUTH_USER
# / BASIC_AUTH_PASS in .env to turn it on. The cookie is signed with
# SECRET_KEY (auto-generated + persisted to .secret_key if unset).
_SECRET_PATH = Path(__file__).parent / ".secret_key"
_sk = getattr(config, "SECRET_KEY", "") or os.environ.get("SECRET_KEY", "")
if not _sk:
    if _SECRET_PATH.exists():
        _sk = _SECRET_PATH.read_text().strip()
    else:
        _sk = secrets.token_hex(32)
        try:
            _SECRET_PATH.write_text(_sk)
            _SECRET_PATH.chmod(0o600)
        except Exception:
            pass
app.secret_key = _sk
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # secure cookie only when served over https (behind nginx+certbot)
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,  # 30 days for "remember me"
)

# Ops/monitoring credential — NOT a real user login. Kept only so the
# runbook's own curl commands (deploy/HOW-TO-UPDATE.md's fetch-queue/status
# etc.) keep working via HTTP Basic auth. Renamed from the old
# BASIC_AUTH_USER/PASS (which used to double as the app's only login) to
# make that distinction explicit — a caller authenticating this way is
# always treated as admin (g.is_admin=True, g.user_id=None).
_OPS_USER = getattr(config, "OPS_API_USER", "") or getattr(config, "BASIC_AUTH_USER", "") or ""
_OPS_PASS = getattr(config, "OPS_API_PASS", "") or getattr(config, "BASIC_AUTH_PASS", "") or ""


def _check_ops_credentials(u: str, p: str) -> bool:
    if not (_OPS_USER and _OPS_PASS):
        return False
    return (
        hmac.compare_digest((u or "").strip(), _OPS_USER)
        and hmac.compare_digest((p or ""), _OPS_PASS)
    )


# paths that never require auth
_AUTH_ALLOW_PREFIXES = ("/login", "/api/login", "/api/logout", "/favicon", "/assets/",
                        "/logo.svg", "/favicon.svg", "/apple-touch-icon.png",
                        "/favicon-16.png", "/favicon-32.png", "/privacy.html", "/terms.html")


@app.route("/api/login", methods=["POST"])
@limiter.limit("5 per minute")
def api_login():
    data = request.get_json(silent=True) or request.form
    email = (data.get("email") or data.get("username") or "").strip()
    password = data.get("password", "")
    conn = db.get_connection()
    try:
        user = conn.execute(
            "SELECT id, email, password_hash, is_admin, display_name, is_active FROM users WHERE lower(email) = lower(%s)",
            (email,),
        ).fetchone()
        if user and user["is_active"] and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session.permanent = bool(data.get("remember"))
            conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user["id"],))
            conn.commit()
            return jsonify({"ok": True, "user": {
                "id": user["id"], "email": user["email"],
                "display_name": user["display_name"], "is_admin": user["is_admin"],
                "fleet_control_enabled": config.FLEET_CONTROL_ENABLED,
            }})
    finally:
        conn.close()
    return jsonify({"ok": False, "error": "Invalid email or password."}), 401


@app.route("/api/logout", methods=["POST", "GET"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET"])
def api_me():
    user = getattr(g, "user", None)
    if not user:
        return jsonify({"auth": False, "user": None})
    return jsonify({"auth": True, "user": {
        "id": user["id"], "email": user["email"],
        "display_name": user["display_name"], "is_admin": user["is_admin"],
        "fleet_control_enabled": config.FLEET_CONTROL_ENABLED,
    }})


# Every field the Trademarks table column picker is allowed to offer — never
# trust arbitrary client-supplied keys straight into a saved preference.
# App Number is deliberately absent: it's always shown first, never optional.
_VALID_TRADEMARK_COLUMNS = {
    "word_mark", "class_number", "application_date", "portal_status",
    "proprietor_name", "sub_status", "tm_type", "state", "valid_upto",
    "publication_date", "class_description", "category",
    "application_filed_as", "office", "country", "certificate_detail",
}


@app.route("/api/me/column-prefs", methods=["GET"])
def get_column_prefs():
    if g.user_id is None:
        return jsonify({"columns": None})
    conn = db.get_connection()
    try:
        columns = db.get_user_column_prefs(conn, g.user_id)
    finally:
        conn.close()
    return jsonify({"columns": columns})


@app.route("/api/me/column-prefs", methods=["POST"])
def set_column_prefs():
    if g.user_id is None:
        return jsonify({"error": "Sign in to save column preferences."}), 401
    data = request.get_json(silent=True) or {}
    columns = data.get("columns")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        return jsonify({"error": "columns must be a list of strings."}), 400
    columns = [c for c in columns if c in _VALID_TRADEMARK_COLUMNS]
    conn = db.get_connection()
    try:
        db.set_user_column_prefs(conn, g.user_id, columns)
    finally:
        conn.close()
    return jsonify({"ok": True, "columns": columns})


@app.before_request
def _require_login():
    path = request.path
    if config.FLEET_CONTROL_ENABLED and (path.startswith('/api/fleet/') or path in (
            '/api/fleet-status', '/api/fetch-queue/status') or path.startswith('/api/fleet-')):
        return jsonify(error='Legacy fleet management is disabled. Use /api/control/v1/.'), 410
    if any(path == p or path.startswith(p) for p in _AUTH_ALLOW_PREFIXES):
        return None

    user_id = session.get("user_id")
    if user_id is not None:
        conn = db.get_connection()
        try:
            user = conn.execute(
                "SELECT id, email, display_name, is_admin, is_active FROM users WHERE id = %s", (user_id,),
            ).fetchone()
        finally:
            conn.close()
        # A deactivated/deleted account's stale cookie is caught here every
        # request, not just trusted because the session claims a user_id.
        if user and user["is_active"]:
            g.user = user
            g.user_id = user["id"]
            g.is_admin = user["is_admin"]
            sentry_sdk.set_user({"id": user["id"], "email": user["email"]})
            return None
        session.clear()

    # Ops/monitoring credential (see _check_ops_credentials) — treated as
    # admin, no per-request users-table lookup needed.
    hdr = request.headers.get("Authorization", "")
    if hdr.startswith("Basic "):
        try:
            import base64 as _b64
            bu, _, bp = _b64.b64decode(hdr[6:]).decode("utf-8", "replace").partition(":")
            if _check_ops_credentials(bu, bp):
                g.user = None
                g.user_id = None
                g.is_admin = True
                return None
        except Exception:
            pass

    # API calls get 401 JSON; browser navigations get redirected to the
    # React login page (there's no server-rendered login page anymore).
    if path.startswith("/api/") or request.headers.get("Accept", "").startswith("application/json"):
        return jsonify({"error": "auth required", "login": "/login"}), 401
    return redirect(f"/login?next={request.full_path if request.query_string else path}")


def effective_user_filter() -> int | None:
    """None => caller is admin, every db.py list/read function below treats
    this as \"no per-tenant filter, show everything\". A real id => scope to
    that user's own data. Use this everywhere instead of scattering
    `if g.is_admin: ... else: ...` branches through route handlers."""
    return None if g.is_admin else g.user_id


def require_admin():
    """Call at the top of a route that must never run for a regular tenant
    (portfolio-wide bulk ops) — returns a 403 response if the caller isn't
    admin, or None if they are (so the route can continue)."""
    if not g.is_admin:
        return jsonify({"error": "Admin access required."}), 403
    return None


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/api/users", methods=["GET"])
def list_users():
    """Admin-only — every account and how many trademarks each one owns,
    for the Manage Users screen. Signup is invite-only (see /api/users
    POST below), so this is also the only way to see who has access."""
    denied = require_admin()
    if denied:
        return denied
    conn = db.get_connection()
    try:
        users = db.list_users(conn)
    finally:
        conn.close()
    return jsonify({"users": users})


@app.route("/api/users", methods=["POST"])
def create_user():
    """Admin-only — creates a new TM Manager account. There is no public
    signup page (see the project's invite-only decision); this is the only
    way new accounts get made, whether by hand here or via the
    scripts/create_user.py CLI equivalent."""
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    display_name = (data.get("display_name") or "").strip() or None
    is_admin = bool(data.get("is_admin"))

    if not email or not _EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    conn = db.get_connection()
    try:
        user_id, err = db.create_user(
            conn, email, generate_password_hash(password), is_admin=is_admin, display_name=display_name,
        )
    finally:
        conn.close()
    if err:
        return jsonify({"error": "That email is already registered."}), 409
    return jsonify({"ok": True, "id": user_id})


@app.route("/api/users/<int:user_id>/active", methods=["POST"])
def set_user_active(user_id):
    """Admin-only — deactivate or reactivate an account. A deactivated
    account is signed out on its very next request (see _require_login)."""
    denied = require_admin()
    if denied:
        return denied
    if user_id == g.user_id:
        return jsonify({"error": "You can't deactivate your own account."}), 400
    data = request.get_json(silent=True) or {}
    is_active = bool(data.get("is_active", True))
    conn = db.get_connection()
    try:
        ok = db.set_user_active(conn, user_id, is_active)
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "User not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/users/<int:user_id>/admin", methods=["POST"])
def set_user_admin(user_id):
    """Admin-only — grant or revoke admin access on an existing account."""
    denied = require_admin()
    if denied:
        return denied
    if user_id == g.user_id:
        return jsonify({"error": "You can't change your own admin status."}), 400
    data = request.get_json(silent=True) or {}
    is_admin = bool(data.get("is_admin", True))
    conn = db.get_connection()
    try:
        ok = db.set_user_admin(conn, user_id, is_admin)
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "User not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/users/<int:user_id>", methods=["PATCH"])
def update_user(user_id):
    """Admin-only — edit an existing account's email/display name. Password
    is deliberately a separate endpoint (see set_user_password below) so a
    name/email save can never accidentally blank or skip a password change,
    and the two actions read as distinct in an audit/log sense."""
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    display_name = (data.get("display_name") or "").strip() or None
    if not email or not _EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    conn = db.get_connection()
    try:
        ok, err = db.update_user(conn, user_id, email, display_name)
    finally:
        conn.close()
    if err == "user not found":
        return jsonify({"error": "User not found."}), 404
    if err:
        return jsonify({"error": "That email is already registered to another account."}), 409
    return jsonify({"ok": True})


@app.route("/api/users/<int:user_id>/password", methods=["POST"])
def set_user_password(user_id):
    """Admin-only — reset a user's password (the admin-UI equivalent of
    scripts/set_password.py). No "current password" check — this is an
    admin resetting someone ELSE's password, not a self-service change
    (there's no such flow yet), same trust boundary as every other action
    on this page."""
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    conn = db.get_connection()
    try:
        ok = db.set_user_password(conn, user_id, generate_password_hash(password))
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "User not found."}), 404
    return jsonify({"ok": True})


# ── Billing/plans admin (see db.py's BILLING_SCHEMA, tm_manager/billing.py) ──

@app.route("/api/admin/plans", methods=["GET"])
def list_plans():
    denied = require_admin()
    if denied:
        return denied
    conn = db.get_connection()
    try:
        plans = db.list_plans(conn)
    finally:
        conn.close()
    return jsonify({"plans": plans})


@app.route("/api/admin/plans", methods=["POST"])
def create_plan():
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    tm_limit = data.get("tm_limit")
    seat_limit = data.get("seat_limit")
    search_limit = data.get("search_limit")
    billing_period_months = data.get("billing_period_months", 1)
    if not name:
        return jsonify({"error": "Plan name is required."}), 400
    if not isinstance(tm_limit, int) or tm_limit < 0:
        return jsonify({"error": "tm_limit must be a non-negative integer."}), 400
    if not isinstance(seat_limit, int) or seat_limit < 0:
        return jsonify({"error": "seat_limit must be a non-negative integer."}), 400
    if search_limit is not None and (not isinstance(search_limit, int) or search_limit < 0):
        return jsonify({"error": "search_limit must be a non-negative integer, or omitted for unlimited."}), 400
    if not isinstance(billing_period_months, int) or billing_period_months not in (1, 3, 6, 12):
        return jsonify({"error": "billing_period_months must be 1, 3, 6, or 12."}), 400
    conn = db.get_connection()
    try:
        plan_id = db.create_plan(
            conn, name, tm_limit, seat_limit,
            price_cents=data.get("price_cents"), features=data.get("features"),
            search_limit=search_limit, billing_period_months=billing_period_months,
        )
    finally:
        conn.close()
    return jsonify({"ok": True, "id": plan_id})


@app.route("/api/admin/plans/<int:plan_id>", methods=["PATCH"])
def update_plan(plan_id):
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    conn = db.get_connection()
    try:
        ok = db.update_plan(
            conn, plan_id,
            name=data.get("name"), price_cents=data.get("price_cents"),
            tm_limit=data.get("tm_limit"), seat_limit=data.get("seat_limit"),
            features=data.get("features"), is_active=data.get("is_active"),
            search_limit=data.get("search_limit"), billing_period_months=data.get("billing_period_months"),
        )
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "Plan not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/admin/subscriptions", methods=["GET"])
def list_subscriptions():
    """Admin-only — every paying account's subscription, joined with its
    plan and a live trademark count, for the billing admin page (usage vs
    limit at a glance, e.g. "180/250 TMs used")."""
    denied = require_admin()
    if denied:
        return denied
    conn = db.get_connection()
    try:
        subs = db.list_subscriptions(conn)
    finally:
        conn.close()
    return jsonify({"subscriptions": subs})


@app.route("/api/admin/subscriptions", methods=["POST"])
def upsert_subscription():
    """Admin-only — assign or replace the one subscription a paying account
    can have (see db.upsert_subscription). Used both to put a brand-new
    account on a plan and to edit an existing account's overrides — the
    frontend always sends the full desired state (plan_id/status/renews_at/
    overrides), not a partial patch, since db.upsert_subscription itself
    doesn't merge with what's already there."""
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    plan_id = data.get("plan_id")
    if not isinstance(user_id, int) or not isinstance(plan_id, int):
        return jsonify({"error": "user_id and plan_id are required."}), 400
    renews_at = data.get("renews_at")
    if renews_at:
        try:
            renews_at = datetime.datetime.fromisoformat(renews_at)
        except ValueError:
            return jsonify({"error": "renews_at must be an ISO date/datetime string."}), 400
    else:
        renews_at = None
    conn = db.get_connection()
    try:
        sub_id = db.upsert_subscription(
            conn, user_id, plan_id,
            status=data.get("status") or "active", renews_at=renews_at,
            tm_limit_override=data.get("tm_limit_override"),
            seat_limit_override=data.get("seat_limit_override"),
            search_limit_override=data.get("search_limit_override"),
            feature_overrides=data.get("feature_overrides"),
        )
    finally:
        conn.close()
    return jsonify({"ok": True, "id": sub_id})


_APP_NO_RE = re.compile(r"^\d{5,10}$")

# Same on-disk data folder the fetching backend writes logos into — both
# apps run on the same machine, so this app can serve them directly without
# ever surfacing that backend's own address to the browser/user.
_DATA_DIR = Path(__file__).parent.parent / "data"

# Where journal PDFs live, keyed by <journal_no>.pdf once parsing reveals
# the number — journal_entry_logo re-opens the PDF by that name whenever a
# logo is requested, so the file has to still be here, not just the parsed
# rows in the DB.
_JOURNAL_DIR = Path(__file__).parent / "Journal"

# Rendered-logo cache for journal entries, same reasoning as the trademark
# logo cache below: rendering a PDF page to an image is comparatively slow,
# so each (journal, page) only gets rendered once and reused after that.
_JOURNAL_LOGO_CACHE_DIR = Path(__file__).parent / "_journal_logo_cache"


def _find_app_no_line_top(page) -> float | None:
    """Returns the y-position (in PDF points) of the "<app_no> <date>"
    anchor line on this page — the logo artwork always occupies the region
    above it, below the journal header line. Groups extracted words into
    lines by their rounded top coordinate (pdfplumber gives per-word
    positions, not per-line) since that's cheap and reliable for this
    journal's consistent layout."""
    lines: dict[float, list[str]] = {}
    for w in page.extract_words():
        key = round(w["top"], 1)
        lines.setdefault(key, []).append(w["text"])
    for top in sorted(lines):
        if parse_journal.APP_NO_DATE_RE.match(" ".join(lines[top])):
            return top
    return None


def _render_journal_entry_logo(pdf_path: Path, page_number: int) -> bytes | None:
    cache_path = _JOURNAL_LOGO_CACHE_DIR / f"{pdf_path.stem}_{page_number}.png"
    if cache_path.exists():
        return cache_path.read_bytes()

    with pdfplumber.open(pdf_path) as pdf:
        if page_number < 1 or page_number > len(pdf.pages):
            return None
        page = pdf.pages[page_number - 1]
        app_no_top = _find_app_no_line_top(page)
        if app_no_top is None or app_no_top <= 65:
            # No room above the app-no line for a logo — a page with no
            # word-mark line AND no artwork above it isn't a real entry to
            # render (shouldn't normally happen for a page that made it
            # into journal_entries, but guards against a malformed crop).
            return None
        crop_box = (0, 60, page.width, app_no_top - 2)
        cropped = page.crop(crop_box)
        image = cropped.to_image(resolution=200)
        buf = BytesIO()
        image.save(buf, format="PNG")
        result = buf.getvalue()

    _JOURNAL_LOGO_CACHE_DIR.mkdir(exist_ok=True)
    cache_path.write_bytes(result)
    return result


def _run_journal_upload_job(
    saved_paths: list[Path],
    job_id: int,
    journal_no: str,
    journal_date: str | None,
) -> None:
    """Runs in a background thread — each file in saved_paths is already at
    its final on-disk name (see journal_upload), so this just hands them to
    parse_journal.run_journal_batch_job, which does the actual parsing/
    storing/matching and updates the journal_jobs row as it goes. On
    failure, removes the files that were uploaded for this attempt rather
    than leaving unparsed PDFs behind with no matching database rows."""
    try:
        parse_journal.run_journal_batch_job(
            [str(p) for p in saved_paths],
            job_id=job_id,
            journal_no_override=journal_no,
            journal_date_override=journal_date,
        )
    except Exception:
        logging.exception("Journal upload job %s failed", job_id)
        for path in saved_paths:
            path.unlink(missing_ok=True)
        temp_path.unlink(missing_ok=True)

# Cropped-logo cache — the scraped logo crops carry a thin printed table
# border (from the source PDF) plus a lot of blank canvas around the actual
# mark, so every logo gets auto-cropped to its real content once and the
# result cached here, keyed by the source file's mtime so a re-fetched logo
# gets reprocessed automatically.
_LOGO_CACHE_DIR = Path(__file__).parent / "_logo_cache"


def _crop_logo_to_content(source_path: Path) -> bytes:
    """Trims the scraped logo crop down to just the mark itself — strips
    the ~1-3px printed border line the source PDF's table cell leaves on
    the bottom/right edges, then crops to the bounding box of non-white
    content with a small padding margin, so a mark that only occupies the
    left slice of a wide crop doesn't drag a huge blank stretch along with
    it wherever the logo is displayed."""
    cache_path = _LOGO_CACHE_DIR / f"{source_path.stat().st_mtime_ns}_{source_path.name}.png"
    if cache_path.exists():
        return cache_path.read_bytes()

    im = Image.open(source_path).convert("RGB")
    arr = np.array(im)
    h, w = arr.shape[:2]

    # The known border artifact: a 1px line along the very bottom edge and
    # a couple of thin vertical lines within the last ~15px on the right —
    # excluded before computing content bounds so they can't anchor the
    # crop to the full canvas size.
    border_margin = 15
    inner = arr[: max(1, h - 2), : max(1, w - border_margin)]
    mask = (inner < 235).any(axis=2)
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)

    if not rows.any() or not cols.any():
        # Nothing but background found (a blank/corrupt scan) — fall back
        # to the original image rather than producing an empty crop.
        buf = BytesIO()
        im.save(buf, format="PNG")
        result = buf.getvalue()
    else:
        row_idx = np.where(rows)[0]
        col_idx = np.where(cols)[0]
        rmin, rmax = int(row_idx[0]), int(row_idx[-1])
        cmin, cmax = int(col_idx[0]), int(col_idx[-1])
        pad = 6
        box = (
            max(0, cmin - pad),
            max(0, rmin - pad),
            min(w, cmax + 1 + pad),
            min(h, rmax + 1 + pad),
        )
        cropped = im.crop(box)
        buf = BytesIO()
        cropped.save(buf, format="PNG")
        result = buf.getvalue()

    _LOGO_CACHE_DIR.mkdir(exist_ok=True)
    cache_path.write_bytes(result)
    return result


def _slugify(text: str) -> str:
    slug = text.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-") or "search"




def _split_app_nos(raw: str) -> list[str]:
    """Accepts newline- or comma-separated application numbers, strips
    whitespace, drops blanks, de-dupes while preserving first-seen order."""
    tokens = re.split(r"[\n,]+", raw)
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        t = tok.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


_ROWS_PER_PAGE = 100


@app.route("/api/dashboard", methods=["GET"])
def index():
    """Landing dashboard — portfolio summary, recent status-change feed, and
    a filing-count chart. Distinct from trademarks_list (the full table),
    same split as the sidebar's Overview vs Trademarks sections."""
    uid = effective_user_filter()
    conn = db.get_connection()
    try:
        counts = db.count_by_status(conn, uid)
        total = sum(counts.values())
        status_changes = db.list_recent_status_changes(conn, uid)
        filing_counts = db.monthly_filing_counts(conn, uid, months=30)
        portal_status_counts = db.count_by_portal_status(conn, uid)
        class_counts = db.count_by_class(conn, uid)
        state_counts = db.count_by_state(conn, uid)
        recent_docs = db.list_correspondence(conn, uid, limit=10)
        recent_reminders = db.list_reminders(conn, uid, limit=5)
        error_count = db.count_error_trademarks(conn, uid)
        opposed_count = db.count_opposed(conn, uid)
        renewals = db.upcoming_renewals(conn, uid, days_ahead=30)
    finally:
        conn.close()

    renewals_by_date: dict[str, list] = {}
    for row in renewals:
        renewals_by_date.setdefault(row["due_date"], []).append(row)

    return jsonify({
        "total": total,
        "counts": counts,
        "status_changes": status_changes,
        "filing_counts": filing_counts,
        "portal_status_counts": portal_status_counts,
        "class_counts": class_counts,
        "state_counts": state_counts,
        "recent_docs": recent_docs,
        "recent_reminders": recent_reminders,
        "error_count": error_count,
        "opposed_count": opposed_count,
        "upcoming_renewals": {
            "by_date": renewals_by_date,
            "total_30d": len(renewals),
        },
    })


_VALID_TM_KINDS = {"portfolio", "opposition_out"}


@app.route("/api/trademarks", methods=["GET"])
def trademarks_list():
    uid = effective_user_filter()
    # kind: 'portfolio' (default, your own marks) or 'opposition_out'
    # (applications your team is opposing — see MY_TRADEMARKS_SCHEMA). Same
    # route, same filters/pagination/columns either way — only which bucket
    # of my_trademarks rows it reads changes. An unrecognized value quietly
    # falls back to 'portfolio' rather than erroring, same as an unset one.
    kind = request.args.get("kind", "portfolio").strip()
    if kind not in _VALID_TM_KINDS:
        kind = "portfolio"
    conn = db.get_connection()
    try:
        # The caller's own saved column choice decides whether list_trademarks
        # bothers with the expensive e-Main-page re-parse this request — a
        # user who never added Office/Country/Certificate Detail to their
        # view never pays that cost (see list_trademarks' extra_columns).
        my_column_prefs = db.get_user_column_prefs(conn, g.user_id) if g.user_id else None
        trademarks = db.list_trademarks(conn, uid, extra_columns=my_column_prefs, kind=kind)
        counts = db.count_by_status(conn, uid, kind=kind)
    finally:
        conn.close()

    # Filters — all optional, applied in Python (see db.list_trademarks'
    # docstring for why: portal_status/class_number/word_mark only exist
    # after that function unpacks each row's data_json, so there's no plain
    # SQL column to filter on without a second, more complex query).
    q = request.args.get("q", "").strip().lower()
    class_filter = request.args.get("class", "").strip()
    status_filter = request.args.get("status", "").strip()
    state_filter = request.args.get("state", "").strip()
    # DD/MM/YYYY, same format application_date is already stored in — kept
    # as plain strings and only converted to a comparable (year, month, day)
    # tuple at filter time, since application_date isn't a real SQL column
    # to filter on (see db.list_trademarks' docstring).
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()

    def _date_key(date_str: str) -> tuple[str, str, str] | None:
        parts = date_str.split("/")
        if len(parts) != 3:
            return None
        day, month, year = parts
        return (year, month, day)

    filtered = trademarks
    if q:
        filtered = [
            tm for tm in filtered
            if q in (tm.get("word_mark") or "").lower()
            or q in (tm.get("app_no") or "").lower()
            or q in (tm.get("proprietor_name") or "").lower()
        ]
    if class_filter:
        filtered = [tm for tm in filtered if (tm.get("class_number") or "") == class_filter]
    if status_filter:
        filtered = [tm for tm in filtered if (tm.get("portal_status") or "") == status_filter]
    if state_filter:
        filtered = [tm for tm in filtered if (tm.get("state") or "") == state_filter]
    if date_from:
        from_key = _date_key(date_from)
        if from_key:
            filtered = [tm for tm in filtered if (_date_key(tm.get("application_date") or "") or ("", "", "")) >= from_key]
    if date_to:
        to_key = _date_key(date_to)
        if to_key:
            filtered = [tm for tm in filtered if (_date_key(tm.get("application_date") or "") or ("9999", "99", "99")) <= to_key]

    # Distinct values for the Filters dropdown — from the full unfiltered
    # set so choosing one filter doesn't hide the options for another.
    available_classes = sorted(
        {tm["class_number"] for tm in trademarks if tm.get("class_number")},
        key=lambda c: (len(c), c),
    )
    available_statuses = sorted({tm["portal_status"] for tm in trademarks if tm.get("portal_status")})

    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    total_filtered = len(filtered)
    total_pages = max(1, (total_filtered + _ROWS_PER_PAGE - 1) // _ROWS_PER_PAGE)
    page = min(page, total_pages)
    start = (page - 1) * _ROWS_PER_PAGE
    page_rows = filtered[start:start + _ROWS_PER_PAGE]

    return jsonify({
        "trademarks": page_rows,
        "counts": counts,
        "total": len(trademarks),
        "total_filtered": total_filtered,
        "q": request.args.get("q", ""),
        "class_filter": class_filter,
        "status_filter": status_filter,
        "state_filter": state_filter,
        "available_classes": available_classes,
        "available_statuses": available_statuses,
        "date_from": date_from,
        "date_to": date_to,
        "page": page,
        "total_pages": total_pages,
        "rows_per_page": _ROWS_PER_PAGE,
    })


@app.route("/api/trademarks/delete", methods=["POST"])
def trademarks_delete():
    """Delete one or more selected records from the tracked portfolio."""
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"error": "Select at least one trademark to delete."}), 400
    if len(raw_ids) > _ROWS_PER_PAGE:
        return jsonify({"error": f"You can delete at most {_ROWS_PER_PAGE} trademarks at once."}), 400

    ids: list[int] = []
    for raw_id in raw_ids:
        if isinstance(raw_id, bool):
            return jsonify({"error": "Invalid trademark selection."}), 400
        try:
            id_ = int(raw_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid trademark selection."}), 400
        if id_ <= 0:
            return jsonify({"error": "Invalid trademark selection."}), 400
        if id_ not in ids:
            ids.append(id_)

    conn = db.get_connection()
    try:
        deleted = db.remove_trademarks(conn, ids, effective_user_filter())
    finally:
        conn.close()
    return jsonify({"ok": True, "deleted": deleted})


_EXPORT_COLUMNS = [
    "Application Number", "Word Mark", "Class", "Status", "Application Date",
    "Owner", "Owner Address", "Attorney", "Attorney Address", "Alert",
    "User Detail", "Expires On", "State", "Office", "Country", "Class Detail",
    "Certificate", "TM Type", "Filing Mode", "Category", "Latest Hearing Date",
    "Updated At", "Image URL", "Slug",
]


def _export_row(tm: dict, data: dict | None) -> list:
    """One row for the portfolio CSV export, matching _EXPORT_COLUMNS in
    order. `data` is get_trademark_data's merged result (None if this mark
    has never been fetched yet) — every field not tracked anywhere in this
    app (Alert, User Detail, Slug) is always blank rather than guessed."""
    data = data or {}
    image_url = f"{request.url_root.rstrip('/')}/trademark/{tm['id']}/logo" if data.get("has_logo") else ""
    return [
        tm.get("app_no", ""),
        data.get("trade_mark") or tm.get("label") or "",
        data.get("class_number", ""),
        data.get("status", ""),
        data.get("application_date", ""),
        data.get("applicant_name", ""),
        data.get("applicant_address", ""),
        data.get("agent_name", ""),
        data.get("agent_address", ""),
        "",  # Alert — not tracked
        "",  # User Detail — not tracked
        data.get("valid_upto", ""),
        data.get("state", ""),
        data.get("office", ""),
        data.get("country", ""),
        data.get("class_description", ""),
        data.get("certificate_detail", ""),
        data.get("tm_type", ""),
        data.get("application_filed_as", ""),
        data.get("category", ""),
        "",  # Latest Hearing Date — not tracked
        tm.get("updated_at").isoformat() if tm.get("updated_at") else "",
        image_url,
        "",  # Slug — not tracked
    ]


@app.route("/api/trademarks/export", methods=["POST"])
def trademarks_export():
    """CSV export of selected portfolio trademarks — same column set the
    user already exports elsewhere (Application Number, Word Mark, Class,
    Status, ... Slug), so a spreadsheet from here drops into the same
    workflow. Reuses get_trademark_data per mark (the same single source
    of truth the detail page reads) rather than a second bespoke query, so
    the export can never disagree with what the app itself shows."""
    import csv
    from io import StringIO

    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"error": "Select at least one trademark to export."}), 400

    ids: list[int] = []
    for raw_id in raw_ids:
        if isinstance(raw_id, bool):
            return jsonify({"error": "Invalid trademark selection."}), 400
        try:
            id_ = int(raw_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid trademark selection."}), 400
        if id_ <= 0:
            return jsonify({"error": "Invalid trademark selection."}), 400
        if id_ not in ids:
            ids.append(id_)

    uid = effective_user_filter()
    conn = db.get_connection()
    try:
        rows = []
        for id_ in ids:
            tm = db.get_trademark(conn, id_, uid)
            if tm is None:
                continue  # not found, or not owned by this user — silently skipped
            data = db.get_trademark_data(conn, tm["app_no"], tm["last_search_id"]) if tm.get("last_search_id") else None
            rows.append(_export_row(tm, data))
    finally:
        conn.close()

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(_EXPORT_COLUMNS)
    writer.writerows(rows)

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=trademarks-export.csv"},
    )


@app.route("/api/add", methods=["POST"])
def add_submit():
    single = request.form.get("app_no", "") or request.json.get("app_no", "") if request.json else ""
    single = single.strip()
    bulk = request.form.get("app_nos_bulk", "") or request.json.get("app_nos_bulk", "") if request.json else ""
    bulk = bulk.strip()
    # kind: 'portfolio' (default — the "My Applications" tab) or
    # 'opposition_out' (the "Outbound Oppositions" tab) — see
    # MY_TRADEMARKS_SCHEMA and _VALID_TM_KINDS. Same add flow either way,
    # including the same immediate fast-fetch below, so an outbound
    # opposition target gets fetched and refreshed exactly like a portfolio
    # mark does.
    kind = request.form.get("kind", "") or (request.json.get("kind", "") if request.json else "")
    kind = kind.strip() or "portfolio"
    if kind not in _VALID_TM_KINDS:
        kind = "portfolio"

    candidates: list[str] = []
    if single:
        candidates.append(single)
    if bulk:
        candidates.extend(_split_app_nos(bulk))

    # De-dupe the combined list too, preserving order.
    seen: set[str] = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    invalid = [c for c in candidates if not _APP_NO_RE.match(c)]
    valid = [c for c in candidates if _APP_NO_RE.match(c)]

    if not candidates:
        return jsonify({"error": "Enter at least one application number.", "added": None, "skipped": None, "invalid": None}), 400

    if g.user_id is None:
        # The ops/monitoring credential (see _check_ops_credentials) has no
        # real user row to own new trademarks — only a real logged-in
        # account (regular or admin) can add to a portfolio.
        return jsonify({"error": "Sign in to add trademarks.", "added": None, "skipped": None, "invalid": None}), 401

    conn = db.get_connection()
    try:
        ok, reason = billing.check_can_add_trademarks(conn, g.user_id, len(valid))
        if not ok:
            return jsonify({"error": reason, "added": None, "skipped": None, "invalid": None}), 403
        result = db.add_trademarks_bulk(conn, valid, g.user_id, kind=kind)
    finally:
        conn.close()

    # Fetch newly-added marks right away at live-search priority instead of
    # leaving them for the slow batch worker to eventually reach — see
    # scheduler.fast_fetch_new_trademarks's docstring.
    added_ids = result.get("added_ids") or {}
    if added_ids and not config.FLEET_CONTROL_ENABLED:
        scheduler.fast_fetch_new_trademarks(added_ids)

    if invalid:
        result.setdefault("invalid", invalid)

    return jsonify({
        "error": None,
        "added": result.get("added"),
        "skipped": result.get("skipped_existing"),
        "invalid": invalid
    })


_ADD_CSV_MAX_ROWS = 5000  # sanity cap on one upload — not a throughput limit


@app.route("/api/add-csv", methods=["POST"])
@limiter.limit("20 per minute")
def add_csv_submit():
    """Bulk-import application numbers from an uploaded CSV/text file —
    for large lists (hundreds+) where /api/add's per-row fast-fetch fan-out
    (one live-priority thread per new row, see
    scheduler.fast_fetch_new_trademarks) would fire all at once and spam the
    small OTP pool with far more concurrent claims than it has accounts.

    These rows are inserted at bulk priority (below a live UI add, same as
    any pre-existing backlog) and deliberately NOT fast-fetched — they're
    left for the normal auto-fetch worker (scheduler._worker_loop) to pick
    up a batch at a time on its existing pace, same as it does for
    everything else already sitting in the queue. Nothing about the
    scraper or the worker's pacing is changed by this endpoint."""
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Choose a CSV file to upload.", "added": None, "skipped": None, "invalid": None}), 400

    kind = request.form.get("kind", "").strip() or "portfolio"
    if kind not in _VALID_TM_KINDS:
        kind = "portfolio"

    # .xlsx/.xls are binary (a zip archive / OLE file), not text — decoding
    # them as UTF-8 below would just produce garbage and "no numbers found".
    # Extract cell values with openpyxl instead, then feed them through the
    # exact same tokenizer/validation path as a pasted or CSV-uploaded list.
    is_excel = uploaded.filename.lower().endswith((".xlsx", ".xlsm"))
    if is_excel:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(BytesIO(uploaded.read()), read_only=True, data_only=True)
            ws = wb.active
            candidates = _split_app_nos("\n".join(
                str(cell.value) for row in ws.iter_rows() for cell in row
                if cell.value is not None and str(cell.value).strip()
            ))
            wb.close()
        except Exception:
            return jsonify({"error": "Could not read that Excel file. Make sure it's a valid .xlsx file.", "added": None, "skipped": None, "invalid": None}), 400
    elif uploaded.filename.lower().endswith(".xls"):
        return jsonify({"error": "Old-style .xls isn't supported — please save it as .xlsx or .csv and try again.", "added": None, "skipped": None, "invalid": None}), 400
    else:
        try:
            raw = uploaded.read().decode("utf-8-sig", errors="replace")
        except Exception:
            return jsonify({"error": "Could not read the uploaded file as text.", "added": None, "skipped": None, "invalid": None}), 400
        # Reuse the same tokenizer as the paste box — a CSV of bare application
        # numbers (one per line, or comma-separated, optionally multiple columns
        # per line) splits the same way plain pasted text does.
        candidates = _split_app_nos(raw)
    if len(candidates) > _ADD_CSV_MAX_ROWS:
        return jsonify({
            "error": f"That file has {len(candidates)} numbers — please split uploads into batches of {_ADD_CSV_MAX_ROWS} or fewer.",
            "added": None, "skipped": None, "invalid": None,
        }), 400

    invalid = [c for c in candidates if not _APP_NO_RE.match(c)]
    valid = [c for c in candidates if _APP_NO_RE.match(c)]

    if not candidates:
        return jsonify({"error": "No application numbers found in that file.", "added": None, "skipped": None, "invalid": None}), 400

    if g.user_id is None:
        return jsonify({"error": "Sign in to add trademarks.", "added": None, "skipped": None, "invalid": None}), 401

    conn = db.get_connection()
    try:
        ok, reason = billing.check_can_add_trademarks(conn, g.user_id, len(valid))
        if not ok:
            return jsonify({"error": reason, "added": None, "skipped": None, "invalid": None}), 403
        # priority=0: same as any pre-existing backlog row — behind a live
        # UI add (_UI_ADD_PRIORITY), fetched by the batch worker in its own
        # time instead of jumping the queue.
        result = db.add_trademarks_bulk(conn, valid, g.user_id, priority=0, kind=kind)
    finally:
        conn.close()

    if invalid:
        result.setdefault("invalid", invalid)

    return jsonify({
        "error": None,
        "added": result.get("added"),
        "skipped": result.get("skipped_existing"),
        "invalid": invalid,
    })


@app.route("/api/tasks-compliances", methods=["GET"])
def tasks_compliances():
    """Compliance/reminder log — every portal reminder notice on file for a
    portfolio trademark. Not a forward-looking due-date calendar: IP India
    only issues a reminder after something is already due, so this reads
    newest-first as a history, same as the portal itself shows it."""
    conn = db.get_connection()
    try:
        uid = effective_user_filter()
        reminders = db.list_reminders(conn, uid)
        custom_tasks = db.list_custom_tasks(conn, uid)
        event_page = None
        if config.FLEET_CONTROL_ENABLED:
            from tm_manager.fleet_compliance import list_events
            from compliance_facts import parse_date
            start = parse_date(request.args.get('start')) if request.args.get('start') else datetime.date.today() - datetime.timedelta(days=1825)
            end = parse_date(request.args.get('end')) if request.args.get('end') else datetime.date.today() + datetime.timedelta(days=365)
            if not start or not end or end < start or (end - start).days > 3660:
                return jsonify(error='Invalid task date window'), 400
            event_page = list_events(conn, uid, start, end,
                                    limit=min(500, max(1, request.args.get('limit', 200, type=int))),
                                    offset=max(0, request.args.get('offset', 0, type=int)),
                                    app_no=request.args.get('app_no') or None, side=request.args.get('side') or None,
                                    category=request.args.get('category') or None)
            compliance = {}
            for event in event_page['items']:
                compliance.setdefault(event['category'], []).append(event)
            # Notices the registry sent without a stored deadline still belong on the calendar, as estimates.
            if request.args.get('side') in (None, '', 'own_mark'):
                dated = {(e['app_no'], e['category']) for e in event_page['items'] if e.get('due_date')}
                wanted_category, wanted_app = request.args.get('category'), request.args.get('app_no')
                for category, items in db.estimate_notice_deadlines(conn, uid, start, end, dated).items():
                    if wanted_category and wanted_category != category:
                        continue
                    compliance.setdefault(category, []).extend(i for i in items if not wanted_app or i['app_no'] == wanted_app)
        else:
            compliance = db.list_compliance_notices(conn, uid, days_ahead=365, days_back=1825)
    finally:
        conn.close()
    return jsonify({"reminders": reminders, "custom_tasks": custom_tasks, "compliance": compliance, "event_page": event_page})


@app.patch('/api/compliance-events/<int:event_id>')
def update_compliance_event(event_id):
    if not config.FLEET_CONTROL_ENABLED:
        abort(404)
    from tm_manager.fleet_compliance import update_event
    conn = db.get_connection()
    try:
        ok = update_event(conn, event_id, g.user_id, request.get_json() or {})
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    finally:
        conn.close()
    return (jsonify(success=True), 200) if ok else (jsonify(error='Event not found in your portfolio'), 404)


@app.get('/api/compliance-events/<int:event_id>/document')
def compliance_event_document(event_id):
    if not config.FLEET_CONTROL_ENABLED:
        abort(404)
    conn = db.get_connection()
    try:
        row = conn.execute('''SELECT e.app_no,e.source_key FROM fleet_control.compliance_events e
            WHERE e.id=%s AND EXISTS(SELECT 1 FROM my_trademarks t WHERE t.app_no=e.app_no AND t.user_id=%s)''',
                           (event_id, g.user_id)).fetchone()
        if not row:
            abort(404)
        return fleet_documents.serve_source(conn, row['app_no'], row['source_key'])
    finally:
        conn.close()


@app.post('/api/trademark/<int:id_>/proceeding-membership')
def proceeding_membership(id_):
    if not config.FLEET_CONTROL_ENABLED or g.user_id is None:
        abort(404)
    body = request.get_json() or {}
    key = body.get('proceeding_key')
    if not isinstance(key, str) or len(key) > 512:
        return jsonify(error='A proceeding identity is required'), 400
    conn = db.get_connection()
    try:
        tm = db.get_trademark(conn, id_, g.user_id)
        if not tm or tm['kind'] != 'opposition_out':
            abort(404)
        proceeding = conn.execute('SELECT 1 FROM fleet_control.proceedings WHERE app_no=%s AND proceeding_key=%s', (tm['app_no'], key)).fetchone()
        if not proceeding:
            abort(404)
        if body.get('tracked', True) is False:
            conn.execute('DELETE FROM public.fleet_proceeding_memberships WHERE user_id=%s AND app_no=%s AND proceeding_key=%s', (g.user_id, tm['app_no'], key))
        else:
            conn.execute('INSERT INTO public.fleet_proceeding_memberships(user_id,app_no,proceeding_key) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (g.user_id, tm['app_no'], key))
        conn.commit()
    finally:
        conn.close()
    return jsonify(success=True)


@app.get('/api/trademark/<int:id_>/source-document')
def source_document(id_):
    if not config.FLEET_CONTROL_ENABLED:
        abort(404)
    conn = db.get_connection()
    try:
        tm = db.get_trademark(conn, id_, effective_user_filter())
        if not tm:
            abort(404)
        return fleet_documents.serve_source(conn, tm['app_no'], request.args.get('source_key'))
    finally:
        conn.close()


@app.get('/api/trademark/<int:id_>/field-document')
def field_source_document(id_):
    if not config.FLEET_CONTROL_ENABLED:
        abort(404)
    conn = db.get_connection()
    try:
        tm = db.get_trademark(conn, id_, effective_user_filter())
        if not tm:
            abort(404)
        field = conn.execute('SELECT source_sha256,result_id,worker_id FROM fleet_control.field_values WHERE app_no=%s AND field_key=%s',
                             (tm['app_no'], request.args.get('field'))).fetchone()
        if not field or not field['source_sha256']:
            abort(404)
        path = config.CONTROL_ARTIFACT_DIR / field['worker_id'] / field['result_id'] / field['source_sha256']
        if not path.is_file():
            abort(503)
        response = send_file(path, as_attachment=True, download_name='field-source.pdf')
        response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    finally:
        conn.close()


@app.route("/api/tasks", methods=["POST"])
def create_task():
    body = request.json or {}
    title = (body.get("title") or "").strip()
    due_date = (body.get("due_date") or "").strip()
    my_trademark_id = body.get("my_trademark_id")
    if not title or not due_date:
        return jsonify({"error": "title and due_date are required."}), 400
    conn = db.get_connection()
    try:
        task = db.create_custom_task(conn, g.user_id, title, due_date, my_trademark_id)
    finally:
        conn.close()
    if task is None:
        return jsonify({"error": "That trademark was not found in your portfolio."}), 400
    return jsonify(task)


@app.route("/api/tasks/<int:id_>", methods=["PATCH"])
def update_task(id_):
    body = request.json or {}
    fields = {k: body[k] for k in ("title", "due_date", "status", "my_trademark_id") if k in body}
    conn = db.get_connection()
    try:
        try:
            ok = db.update_custom_task(conn, id_, effective_user_filter(), **fields)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "Task not found."}), 404
    return jsonify({"success": True})


@app.route("/api/tasks/<int:id_>", methods=["DELETE"])
def delete_task(id_):
    conn = db.get_connection()
    try:
        ok = db.delete_custom_task(conn, id_, effective_user_filter())
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "Task not found."}), 404
    return jsonify({"success": True})


@app.route("/api/compliance-status", methods=["PATCH"])
def update_compliance_status():
    """Set the current user's own status (Pending/In Progress/Completed/
    Closed) on one read-only registry notice — a reminder or correspondence
    row from list_reminders/list_correspondence, identified by the
    (source, source_id) pair those functions already attach to every row
    (see db.set_compliance_status). Distinct from PATCH /api/tasks/<id>,
    which is for user-created custom_tasks rows, not registry notices."""
    body = request.json or {}
    source = (body.get("source") or "").strip()
    source_id = body.get("source_id")
    status = (body.get("status") or "").strip()
    if not source or not isinstance(source_id, int) or not status:
        return jsonify({"error": "source, source_id and status are required."}), 400
    if g.user_id is None:
        # Same rule as custom tasks — a status is inherently personal, no
        # admin/ops "set for everyone" path.
        return jsonify({"error": "Sign in to update task status."}), 401
    conn = db.get_connection()
    try:
        try:
            ok = db.set_compliance_status(conn, source, source_id, g.user_id, status)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "Notice not found in your portfolio."}), 404
    return jsonify({"success": True})


@app.route("/api/status-changes", methods=["GET"])
def status_changes():
    conn = db.get_connection()
    try:
        changes = db.list_recent_status_changes(conn, effective_user_filter(), limit=200)
    finally:
        conn.close()
    return jsonify({"changes": changes})


@app.route("/api/new-documents", methods=["GET"])
def new_documents():
    conn = db.get_connection()
    try:
        documents = db.list_correspondence(conn, effective_user_filter())
    finally:
        conn.close()
    return jsonify({"documents": documents})


@app.route("/api/journal-watch", methods=["GET"])
def journal_watch():
    """Portfolio trademarks with a similar-sounding mark published in a
    parsed Trade Marks Journal — matches are precomputed by
    scripts/parse_journal.py (not run live here), so this just reads what's
    already in journal_matches. Optional ?journal_no=<n> to view a specific
    past journal instead of the latest one.

    Optional ?min_similarity=<55|70|85> (default 0.55 if omitted entirely,
    e.g. a raw API/curl call — the frontend itself always sends its own
    default of 70%, see JournalWatch.tsx) — passed straight through to
    list_journal_matches' own SQL WHERE clause instead of always fetching
    every 55%+ match and filtering client-side. A journal with thousands
    of low-confidence matches (a large portfolio scored against a large
    journal) used to mean downloading and JSON-serializing all of them on
    every page load/journal switch even when the user had the UI's Min.
    similarity slider raised — this makes raising that slider actually
    shrink the query, not just the client-side view."""
    journal_no = request.args.get("journal_no", "").strip() or None
    min_similarity = request.args.get("min_similarity", 0.55, type=float)
    # Clamp to a sane range rather than trusting the query string outright —
    # this only ever feeds a numeric comparison, but a stray value (e.g. a
    # fraction typo like 55 instead of 0.55) shouldn't silently return
    # nothing or everything.
    min_similarity = max(0.0, min(1.0, min_similarity))
    conn = db.get_connection()
    try:
        matches = db.list_journal_matches(
            conn, effective_user_filter(), min_similarity=min_similarity, journal_no=journal_no,
        )
        journals = db.list_journal_numbers(conn)
        active_journal_no = journal_no or db.latest_journal_no(conn)
    finally:
        conn.close()
    return jsonify({"matches": matches, "journal_no": active_journal_no, "journals": journals})


@app.route("/journal-entry/<int:id_>/logo", methods=["GET"])
def journal_entry_logo(id_):
    """Renders the mark's artwork directly out of the journal PDF page it
    was published on — the journal has no extractable embedded image for a
    device mark (it's drawn as vector content on the page, not a raster
    file), so instead of an <img> reading a saved file this crops+renders
    the page region between the header line and the app-no line, which is
    always exactly where the mark sits (see parse_journal.py's module
    docstring). Cached to disk after the first render."""
    conn = db.get_connection()
    try:
        cursor = conn.execute(
            "SELECT journal_no, page_number, source_file FROM journal_entries WHERE id = %(id)s", {"id": id_}
        )
        entry = cursor.fetchone()
    finally:
        conn.close()
    if entry is None or not entry.get("page_number"):
        abort(404)

    # source_file is the exact PDF this entry's page_number is relative to
    # — a journal split across several class-range files restarts page
    # numbering at 1 in each one, so journal_no alone isn't enough to find
    # the right file (see parse_journal.py's parse_journal_pdf). Falls back
    # to the old <journal_no>.pdf convention for rows written before this
    # column existed.
    filename = entry.get("source_file") or f"{entry['journal_no']}.pdf"
    pdf_path = _JOURNAL_DIR / filename
    if not pdf_path.is_file():
        abort(404)

    try:
        image_bytes = _render_journal_entry_logo(pdf_path, entry["page_number"])
    except Exception:
        logging.exception("Failed to render journal logo for entry %s", id_)
        abort(404)
    if image_bytes is None:
        abort(404)
    return send_file(BytesIO(image_bytes), mimetype="image/png")


@app.route("/api/journal/upload", methods=["POST"])
def journal_upload():
    """Accepts one or more class-range PDFs for a single journal (IP India
    publishes each week's journal split into several files — Class 1-9,
    10-25, 26-34, ... — all sharing one journal number/date), saves them,
    and kicks off parse_journal.run_journal_batch_job in a background
    thread. Parsing a full journal's worth of pages takes several minutes,
    far too long to hold this request open for, so the response just
    returns a job_id the frontend polls via /api/journal/jobs/<job_id>.

    journal_no is required — knowing it upfront (rather than discovering
    it mid-parse, as the old single-file flow did) means every uploaded
    file can be saved under its final name immediately, which is what lets
    journal_entry_logo look a page up by plain (journal_no, source_file)
    instead of guessing at a rename that happens after the fact."""
    journal_no = request.form.get("journal_no", "").strip()
    journal_date = request.form.get("journal_date", "").strip() or None
    if not journal_no:
        return jsonify({"error": "Journal number is required."}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded."}), 400
    for f in files:
        if not f.filename or not f.filename.lower().endswith(".pdf"):
            return jsonify({"error": f"'{f.filename}' isn't a PDF file."}), 400

    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    for i, f in enumerate(files):
        suffix = f"_part{i + 1}" if len(files) > 1 else ""
        dest = _JOURNAL_DIR / f"{journal_no}{suffix}.pdf"
        # A same-named file already on disk gets overwritten deliberately —
        # re-uploading is how you'd correct a bad prior upload for this
        # journal number/part.
        f.save(dest)
        saved_paths.append(dest)

    conn = db.get_connection()
    try:
        job_id = db.create_journal_job(
            conn,
            filename=files[0].filename if len(files) == 1 else f"{len(files)} files",
            total_files=len(files),
            journal_no=journal_no,
            journal_date=journal_date,
        )
    finally:
        conn.close()

    thread = threading.Thread(
        target=_run_journal_upload_job,
        args=(saved_paths, job_id, journal_no, journal_date),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id}), 202


@app.route("/api/journal/jobs/<int:job_id>", methods=["GET"])
def journal_job_status(job_id):
    conn = db.get_connection()
    try:
        job = db.get_journal_job(conn, job_id)
    finally:
        conn.close()
    if job is None:
        abort(404)
    return jsonify(job)


@app.route("/api/journal/jobs/latest", methods=["GET"])
def journal_job_latest():
    """Lets the frontend recover an in-progress job on page load/refresh
    without needing to keep the job_id around client-side."""
    conn = db.get_connection()
    try:
        job = db.get_latest_journal_job(conn)
    finally:
        conn.close()
    return jsonify(job)


@app.route("/api/lookup-cached", methods=["GET"])
def lookup_cached():
    """Instant check for an already-scraped result before TM Search bothers
    the live (and occasionally down/rate-limited) IP India portal at all —
    ?app_no=<n>. Appno search only; word-mark search always needs a live
    hit since a cached search_type='name' row means one specific past
    query string, not "this app_no has been searched by name before"."""
    app_no = request.args.get("app_no", "").strip()
    if not app_no:
        return jsonify({"error": "app_no is required."}), 400
    conn = db.get_connection()
    try:
        search_id = db.find_cached_appno_search(conn, app_no)
        tm_data = db.get_trademark_data(conn, app_no, search_id) if search_id else None
        # Same fix as lookup_result/trademark_detail below — reads
        # tma_documents directly rather than leaving documents out of the
        # cached-hit response, which otherwise always showed "No documents
        # found" for a cache hit even when documents genuinely were saved.
        documents = [
            {"filename": d["filename"], "url": f"/ipindia/document/{_slugify(app_no)}/{d['filename']}"}
            for d in db.list_tma_documents(conn, app_no)
        ] if search_id else []
    finally:
        conn.close()
    return jsonify({"search_id": search_id, "data": tm_data, "documents": documents})


@app.route("/api/search-check", methods=["GET"])
def search_check():
    """Called by the TM Search page BEFORE it does anything else (cache
    lookup or a live scrape) — tells the frontend whether this search is
    allowed under the caller's plan, so a blocked user sees a clear message
    immediately instead of the scraper call just silently going through
    (the scraper itself has no concept of per-user plans/limits — it's
    shared infrastructure, not where billing enforcement belongs). See
    billing.check_can_search."""
    if g.user_id is None:
        return jsonify({"allowed": True})  # ops/Basic-auth callers, no billing concept
    conn = db.get_connection()
    try:
        allowed, reason = billing.check_can_search(conn, g.user_id)
    finally:
        conn.close()
    return jsonify({"allowed": allowed, "reason": reason})


@app.route("/api/search-history", methods=["GET"])
def search_history():
    """Recent TM Search lookups — ?limit=N, default 30 — so the TM Search
    page can show what's been searched before without anyone having to
    remember application numbers. Scoped to the caller's own searches (see
    db.tm_search_history) — admin sees everyone's."""
    limit = request.args.get("limit", 30, type=int)
    conn = db.get_connection()
    try:
        rows = db.list_recent_appno_searches(conn, effective_user_filter(), limit)
    finally:
        conn.close()
    return jsonify({"history": rows})


@app.route("/api/search-history", methods=["POST"])
def record_search_history():
    """Called by the frontend right when a TM Search lookup completes (live
    fetch or cache hit alike) — logs it against the caller's own account.
    See db.record_search_history's docstring for why this lives in TM
    Manager's own table rather than the scraper's shared results table."""
    if g.user_id is None:
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    app_no = (data.get("app_no") or "").strip()
    search_id = data.get("search_id")
    if not app_no:
        return jsonify({"error": "app_no is required."}), 400
    conn = db.get_connection()
    try:
        db.record_search_history(conn, g.user_id, app_no, search_id)
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/lookup-result/<int:search_id>", methods=["GET"])
def lookup_result(search_id):
    """Read-only view of one ad-hoc scrape result by search_id, for the TM
    Search page — a quick lookup that hasn't (yet) been added to the
    portfolio, so there's no my_trademarks row to key off.

    ?search_type=name returns every match as a list straight from
    ipindia_results.data_json (a word-mark search can return many
    trademarks, unlike an app-number search which always returns exactly
    one) — get_trademark_data hardcodes data_json[0], which would silently
    drop every match but the first, so name search bypasses it entirely.

    appno search (default) still goes through get_trademark_data for the
    full single-record detail (correspondence, reminders, TM-A fields) the
    page already knows how to render; app_no must be passed back by the
    caller since get_trademark_data needs it for those joins."""
    search_type = request.args.get("search_type", "appno").strip()
    conn = db.get_connection()
    try:
        if search_type == "name":
            cursor = conn.execute(
                "SELECT data_json FROM ipindia_results WHERE id = %(id)s",
                {"id": search_id},
            )
            row = cursor.fetchone()
            matches = (row["data_json"] if row and row.get("data_json") else []) or []
            return jsonify({"matches": matches})

        app_no = request.args.get("app_no", "").strip()
        if not app_no:
            return jsonify({"error": "app_no is required."}), 400
        tm_data = db.get_trademark_data(conn, app_no, search_id)
    finally:
        conn.close()
    if tm_data is None:
        return jsonify({"error": "Result not found."}), 404

    # Same DB-backed listing /api/trademark/<id> uses — needed here too so
    # TM Search's history/refresh view can show what documents actually got
    # saved. Without this, re-opening a past search from history always
    # showed "No documents found yet." even when documents/PDFs genuinely
    # WERE saved during the original fetch: the frontend's document list
    # only ever came from the live on_doc_saved feed during an in-progress
    # job, which is empty by definition once you're viewing history instead
    # of a job that just ran.
    #
    # Reads tma_documents directly rather than globbing the local disk
    # folder — a document that uploaded straight to Drive (Drive-first
    # upload hook, no local file at all) has no file on disk to glob, but
    # still needs to show up here. The URL still points at the root
    # scraper's /ipindia/document/... route, which is itself Drive-aware
    # (tries Drive first, falls back to local disk).
    conn = db.get_connection()
    try:
        documents = [
            {"filename": d["filename"], "url": f"/ipindia/document/{_slugify(app_no)}/{d['filename']}"}
            for d in db.list_tma_documents(conn, app_no)
        ]
    finally:
        conn.close()

    return jsonify({"data": tm_data, "documents": documents})


@app.route("/api/trademark/<int:id_>", methods=["GET"])
def trademark_detail(id_):
    # Optional ?search_id=<id> to view an older fetch instead of the latest
    # — refetching never overwrites anything, so every past fetch is still
    # there in the shared database and can be viewed by date.
    view_search_id = request.args.get("search_id", type=int)

    uid = effective_user_filter()
    conn = db.get_connection()
    try:
        tm = db.get_trademark(conn, id_, uid)
        tm_data = None
        history = []
        # Show the last successfully fetched data (last_search_id) even
        # while a live re-fetch is in progress (status='fetching') or a
        # retry just failed (status='error') — refetching never overwrites
        # past results, so there's no reason to blank out a page that
        # already has good data just because a refresh is running.
        if tm is not None:
            history = db.list_fetch_history(conn, tm["app_no"])
            search_id = view_search_id or tm["last_search_id"]
            if search_id:
                tm_data = db.get_trademark_data(conn, tm["app_no"], search_id)
            if config.FLEET_CONTROL_ENABLED:
                tm['fleet_schedule'] = conn.execute('SELECT policy_id,last_successful_fetch_at,last_published_at,next_fetch_at FROM fleet_control.trademarks WHERE app_no=%s', (tm['app_no'],)).fetchone()
                tm['fleet_job'] = conn.execute('SELECT state,attempts,last_error FROM fleet_control.jobs WHERE app_no=%s ORDER BY generation DESC LIMIT 1', (tm['app_no'],)).fetchone()
                if tm_data is not None:
                    tm_data['proceedings'] = conn.execute('SELECT opposition_no,portal_type,facts,observed_at FROM fleet_control.proceedings WHERE app_no=%s ORDER BY opposition_no', (tm['app_no'],)).fetchall()
                    for proceeding in tm_data['proceedings']:
                        key = proceeding['facts']['proceeding_key']
                        proceeding['tracked_by_you'] = bool(conn.execute('SELECT 1 FROM public.fleet_proceeding_memberships WHERE user_id=%s AND app_no=%s AND proceeding_key=%s', (g.user_id, tm['app_no'], key)).fetchone())
                    tm_data['document_coverage'] = conn.execute('SELECT source_key,kind,metadata,download_state,parse_state FROM fleet_control.document_ledger WHERE app_no=%s ORDER BY source_key', (tm['app_no'],)).fetchall()
                    tm_data['field_provenance'] = conn.execute('SELECT field_key,source_key,observed_at,source_sha256 FROM fleet_control.field_values WHERE app_no=%s ORDER BY field_key', (tm['app_no'],)).fetchall()
                if tm['fleet_job'] and tm['fleet_job']['state'] == 'failed':
                    tm['status'] = 'error'
                    tm['error_message'] = tm['fleet_job']['last_error']
    finally:
        conn.close()
    if tm is None:
        return jsonify({"error": "Trademark not found."}), 404

    # Reads tma_documents directly rather than globbing the local disk
    # folder — see the identical fix (and its rationale) in lookup_result
    # above. A Drive-first upload with no local file would otherwise never
    # show up here even though trademark_document (used to actually open
    # it) already knows how to serve it from Drive.
    conn = db.get_connection()
    try:
        documents = [d["filename"] for d in db.list_tma_documents(conn, tm["app_no"])]
        status_pc = db.prev_and_current_status(conn, id_, uid)
    finally:
        conn.close()

    return jsonify({
        "tm": tm,
        "data": tm_data,
        "documents": documents,
        "history": history,
        "viewing_search_id": view_search_id or (tm["last_search_id"] if tm else None),
        "status_prev_current": status_pc,
        "fleet_control_enabled": config.FLEET_CONTROL_ENABLED,
    })


@app.route("/api/trademark/<int:id_>/history", methods=["GET"])
def trademark_status_history(id_):
    """Full status/field-change timeline for one mark — every fetch, its
    snapshot, and what changed vs the fetch before it."""
    uid = effective_user_filter()
    conn = db.get_connection()
    try:
        rows = db.list_status_history(conn, id_, uid)
        pc = db.prev_and_current_status(conn, id_, uid)
    finally:
        conn.close()
    return jsonify({"timeline": rows, "prev_current": pc})


@app.route("/api/changes", methods=["GET"])
def portfolio_changes():
    """Portfolio-wide change feed. ?field=status|proprietor_name|... &since=YYYY-MM-DD"""
    field = request.args.get("field", "status")
    since = request.args.get("since") or None
    limit = min(500, request.args.get("limit", 200, type=int))
    conn = db.get_connection()
    try:
        rows = db.list_portfolio_changes(conn, effective_user_filter(), since=since, field=field, limit=limit)
    finally:
        conn.close()
    return jsonify({"changes": rows, "field": field, "since": since})


@app.route("/api/trademark/<int:id_>/notes", methods=["POST"])
def trademark_update_notes(id_):
    notes = request.json.get("notes", "") if request.json else ""
    conn = db.get_connection()
    try:
        ok = db.update_trademark_notes(conn, id_, effective_user_filter(), notes)
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "Trademark not found."}), 404
    return jsonify({"success": True})


def _try_drive_download(file_id: str) -> bytes | None:
    """Best-effort Drive download — returns bytes on success, None on any
    failure (not configured, connect failed, download failed). Never
    raises: callers should fall back to local disk on None, exactly
    mirroring pdf_processor.py's _try_drive_upload on the read side."""
    if not file_id or not config.is_drive_configured():
        return None
    try:
        drive = DriveClient()
        if not drive.connect():
            return None
        return drive.download_bytes(file_id)
    except Exception:
        logging.exception("Drive download failed for file_id=%s", file_id)
        return None


def _serve_logo_for_app_no(app_no: str, drive_file_id: str | None = None):
    """Shared body for both logo routes below.

    Drive-first: if *drive_file_id* is given (the caller already looked up
    tma_documents.logo_drive_file_id or drive_logo_files for this app_no),
    serve that before ever touching local disk — served as-is, without the
    border-crop local files get (that crop's cache is keyed by a local
    file's mtime, which doesn't apply to a Drive-sourced image; skipping
    it here is a minor cosmetic difference, not a correctness issue, same
    "an uncropped logo beats no logo" philosophy the local fallback below
    already uses when cropping itself fails). On any Drive miss/failure,
    falls through to the exact local-disk logic this function always had.

    Prefers trademark-image.png — the portal's own directly-fetched image
    (IPIndiaSession.get_trademark_image, saved by _fetch_all_tm_panels in
    the scraper) — over any *logo*.png/jpg cropped out of a PDF, since the
    PDF crop is a lower-quality fallback for when the direct fetch itself
    failed. The *logo* glob alone never matched this file (different name),
    so a hit with a direct image but no successfully-parsed PDF document
    showed no image at all even though the file was sitting right there —
    the same underlying bug the ipindia_trademark_images table (root db.py)
    was added to fix on the data side; this fixes it on the serving side.
    """
    if drive_file_id:
        drive_bytes = _try_drive_download(drive_file_id)
        if drive_bytes is not None:
            return send_file(BytesIO(drive_bytes), mimetype="image/png")

    folder = _DATA_DIR / "ipindia" / _slugify(app_no)
    if not folder.is_dir():
        abort(404)
    direct_image = folder / "trademark-image.png"
    if direct_image.is_file():
        matches = [direct_image]
    else:
        matches = list(folder.glob("*logo*.png")) + list(folder.glob("*logo*.jpg"))
    if not matches:
        abort(404)
    try:
        cropped = _crop_logo_to_content(matches[0])
    except Exception:
        # Any processing failure (corrupt image, unexpected format) falls
        # back to serving the original file as-is rather than a 500 — an
        # untrimmed logo is still better than none.
        logging.exception("Failed to crop logo for app_no %s", app_no)
        return send_from_directory(folder, matches[0].name, as_attachment=False)
    return send_file(BytesIO(cropped), mimetype="image/png")


def _resolve_logo_drive_id(conn, app_no: str) -> str | None:
    """Best-known Drive id for this app_no's logo, checking sources in the
    same precedence _serve_logo_for_app_no's local fallback already uses
    (direct portal image preferred over a PDF-cropped one): first
    drive_logo_files (trademark-image.png, the direct fetch), then
    tma_documents.logo_drive_file_id (the PDF-crop fallback) for the
    newest row. Either may be None if that source was never uploaded to
    Drive (not configured, or still local-only) — that's the normal case
    for most rows today and the caller falls back to local disk."""
    direct_id = db.get_glob_logo_drive_id(conn, app_no, "trademark-image.png")
    if direct_id:
        return direct_id
    cursor = conn.execute(
        "SELECT logo_drive_file_id FROM tma_documents WHERE app_no = %(app_no)s "
        "ORDER BY extracted_at DESC LIMIT 1",
        {"app_no": app_no},
    )
    row = cursor.fetchone()
    return row["logo_drive_file_id"] if row else None


@app.route("/trademark/<int:id_>/logo", methods=["GET"])
def trademark_logo(id_):
    """Serves the trademark's logo image — Drive first if a Drive copy is
    on file, falling back to the shared local data folder otherwise."""
    conn = db.get_connection()
    try:
        tm = db.get_trademark(conn, id_, effective_user_filter())
        if tm is None:
            abort(404)
        fleet_response = fleet_documents.serve(conn, tm['app_no'], 'logo')
        if fleet_response is not None:
            return fleet_response
        drive_id = _resolve_logo_drive_id(conn, tm["app_no"])
    finally:
        conn.close()
    return _serve_logo_for_app_no(tm["app_no"], drive_file_id=drive_id)


@app.route("/api/lookup-logo/<app_no>", methods=["GET"])
def lookup_logo(app_no):
    """Same logo image, keyed directly by app_no instead of a my_trademarks
    id — for the TM Search page's ad-hoc lookups, which have no portfolio
    row (and thus no id) until the user clicks Save. Deliberately NOT
    scoped by ownership: this is cached public IP India portal data (any
    logged-in user can look up any application number on TM Search itself),
    not private portfolio membership — matches how the rest of the shared
    scrape cache (get_trademark_data, lookup-cached) already works."""
    conn = db.get_connection()
    try:
        drive_id = _resolve_logo_drive_id(conn, app_no)
    finally:
        conn.close()
    return _serve_logo_for_app_no(app_no, drive_file_id=drive_id)


@app.route("/trademark/<int:id_>/document/<filename>", methods=["GET"])
def trademark_document(id_, filename):
    """Serves one of the trademark's saved documents (application PDF) —
    Drive first if this row has a drive_file_id, falling back to local
    disk otherwise. Opens inline in the browser's own PDF viewer (which
    still offers its own Save/Download control) rather than forcing an
    immediate download, same as trademark_logo/trademark_notice below."""
    conn = db.get_connection()
    try:
        tm = db.get_trademark(conn, id_, effective_user_filter())
        if tm is None:
            abort(404)
        fleet_response = fleet_documents.serve(conn, tm['app_no'], 'document', filename)
        if fleet_response is not None:
            return fleet_response
        doc = db.get_tma_document_by_filename(conn, tm["app_no"], filename)
    finally:
        conn.close()

    if doc and doc.get("drive_file_id"):
        drive_bytes = _try_drive_download(doc["drive_file_id"])
        if drive_bytes is not None:
            return send_file(BytesIO(drive_bytes), mimetype="application/pdf",
                              as_attachment=False, download_name=filename)

    folder = _DATA_DIR / "ipindia" / _slugify(tm["app_no"])
    if not folder.is_dir():
        abort(404)
    return send_from_directory(folder, filename, as_attachment=False)


@app.route("/trademark/<int:id_>/notice/<kind>/<filename>", methods=["GET"])
def trademark_notice(id_, kind, filename):
    """Views a correspondence or reminder notice inline — Drive first if
    this row has a drive_file_id, falling back to local disk otherwise.
    These are saved HTML notice pages the portal itself renders, meant to
    be read, not downloaded. kind is 'correspondence' or 'reminders'."""
    if kind not in ("correspondence", "reminders"):
        abort(404)
    conn = db.get_connection()
    try:
        tm = db.get_trademark(conn, id_, effective_user_filter())
        if tm is None:
            abort(404)
        fleet_response = fleet_documents.serve(conn, tm['app_no'], 'reminder' if kind == 'reminders' else 'correspondence', filename)
        if fleet_response is not None:
            return fleet_response
        if kind == "reminders":
            drive_id = db.get_reminder_drive_id(conn, tm["app_no"], filename)
        else:
            drive_id = db.get_correspondence_drive_id(conn, tm["app_no"], filename)
    finally:
        conn.close()

    if drive_id:
        drive_bytes = _try_drive_download(drive_id)
        if drive_bytes is not None:
            mimetype = "text/html" if filename.endswith((".html", ".htm")) else "application/pdf"
            return send_file(BytesIO(drive_bytes), mimetype=mimetype,
                              as_attachment=False, download_name=filename)

    folder = _DATA_DIR / "ipindia" / _slugify(tm["app_no"]) / kind
    if not folder.is_dir():
        abort(404)
    return send_from_directory(folder, filename, as_attachment=False)


@app.route("/trademark/<int:id_>/retry", methods=["POST"])
@limiter.limit("20 per minute")
def trademark_retry(id_):
    uid = effective_user_filter()
    conn = db.get_connection()
    try:
        ok = db.request_fleet_refresh(conn, id_, uid) if config.FLEET_CONTROL_ENABLED else db.reset_trademark_to_pending(conn, id_, uid)
        if ok and not config.FLEET_CONTROL_ENABLED:
            db.bump_trademark_priority(conn, id_, uid)   # jump the queue on a manual retry
    finally:
        conn.close()
    if not ok:
        abort(404)
    return redirect(url_for("trademark_detail", id_=id_))


@app.route("/api/trademark/<int:id_>/live-refresh", methods=["POST"])
@limiter.limit("20 per minute")
def trademark_live_refresh(id_):
    """Fast-lane manual refresh — reuses the same priority-warm-pool path a
    freshly-added trademark already gets (scheduler.fast_fetch_new_trademarks),
    instead of trademark_retry's slow batched auto-fetch queue. Runs the
    actual scrape in a background thread via scheduler._run_fast_fetch_one
    (single-item /api/scrape-appno, not the batch endpoint) and marks the
    row 'fetching' immediately so the existing poll loop in DetailPane.tsx
    (which already watches for status leaving pending/fetching) picks up
    the change without any frontend polling logic changes."""
    uid = effective_user_filter()
    conn = db.get_connection()
    if config.FLEET_CONTROL_ENABLED:
        try:
            ok = db.request_fleet_refresh(conn, id_, uid)
        finally:
            conn.close()
        return (jsonify(ok=True, state='queued'), 202) if ok else (jsonify(error='Trademark not found.'), 404)
    try:
        tm = db.get_trademark(conn, id_, uid)
        if tm is None:
            abort(404)
        db.mark_trademark_fetching(conn, id_)
    finally:
        conn.close()

    threading.Thread(
        target=scheduler._run_fast_fetch_one,
        args=(tm["app_no"], id_),
        name=f"live-refresh-{tm['app_no']}",
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/trademark/<int:id_>/fetch-now", methods=["POST"])
@limiter.limit("20 per minute")
def trademark_fetch_now(id_):
    """Push one mark to the very front of the auto-fetch queue (and re-queue
    it if it had errored)."""
    conn = db.get_connection()
    try:
        ok = db.request_fleet_refresh(conn, id_, effective_user_filter()) if config.FLEET_CONTROL_ENABLED else db.bump_trademark_priority(conn, id_, effective_user_filter())
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "Trademark not found."}), 404
    return jsonify({"ok": True, "id": id_})


@app.route("/api/trademarks/fetch-all-pending", methods=["POST"])
def trademarks_fetch_all_pending():
    """Raise the priority of every 'pending' mark so the whole backlog gets
    worked off ahead of the migrated priority-0 rows. Use after a bulk import
    when you want everything fetched now. Admin-only — this is a
    portfolio-wide operation across every tenant's rows, not something a
    regular user should be able to trigger even against just their own."""
    denied = require_admin()
    if denied:
        return denied
    conn = db.get_connection()
    try:
        n = conn.execute(
            "UPDATE my_trademarks SET priority = GREATEST(priority, 100), updated_at = now() "
            "WHERE status IN ('pending', 'error')"
        ).rowcount
        if config.FLEET_CONTROL_ENABLED:
            for row in conn.execute("SELECT DISTINCT app_no FROM my_trademarks WHERE status IN ('pending','error')").fetchall():
                db.queue_fetch_request(conn, row['app_no'], g.user_id, 'manual', 100)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "raised": n})


@app.route("/api/fetch-queue/status", methods=["GET"])
def fetch_queue_status():
    """One-glance health of the auto-fetch pipeline — how many marks are in
    each state, whether the background worker is even turned on, how many
    are actively fetching right now (a stuck-forever value here usually
    means the worker crashed — check the backend log), and the scraper's own
    warm-pool health so a client-facing dashboard can show 'fetching is slow
    because only 1 lane is warm' instead of just silence. Admin-only — this
    is an ops/monitoring view across every tenant's queue, not a regular
    user's own status."""
    denied = require_admin()
    if denied:
        return denied
    conn = db.get_connection()
    try:
        counts = db.count_by_status(conn, None)
        oldest_pending = conn.execute(
            "SELECT min(created_at) AS t FROM my_trademarks WHERE status = 'pending'"
        ).fetchone()["t"]
        stuck_fetching = conn.execute(
            "SELECT count(*) AS n FROM my_trademarks "
            "WHERE status = 'fetching' AND last_attempted_at < now() - interval '10 minutes'"
        ).fetchone()["n"]
    finally:
        conn.close()

    warm_pool = None
    try:
        r = requests.get(
            f"{config.SCRAPER_API_BASE_URL}/api/warm-pool/status", timeout=5,
            auth=scheduler._SCRAPER_AUTH,
        )
        if r.ok:
            warm_pool = r.json().get("health")
    except Exception:
        pass  # scraper unreachable — surface that as warm_pool: null, not a 500

    return jsonify({
        "counts": counts,
        "oldest_pending_since": oldest_pending,
        "stuck_fetching_count": stuck_fetching,   # >0 for a while = worker likely crashed/stalled
        "auto_fetch_enabled": scheduler._AUTO_FETCH_ENABLED,
        "auto_fetch_workers": scheduler._WORKERS,
        "warm_pool": warm_pool,   # null if the scraper app couldn't be reached at all
    })


# ── Fleet dashboard (Contabo-coordinator + local-worker-fleet) ──────────────
# Admin-only, browser session-cookie auth (same as every route above) — a
# different trust boundary from tm_manager/fleet_api.py's worker-facing
# Basic-Auth endpoints, which is why that lives in its own module. Nothing
# here talks to the scraper or affects Contabo's own live-search scraping.

@app.route("/api/fleet-status", methods=["GET"])
def fleet_status():
    denied = require_admin()
    if denied:
        return denied
    conn = db.get_connection()
    try:
        machines = db.list_fleet_machines(conn, config.FLEET_HEARTBEAT_STALE_SECONDS)
        # Unassigned, assignable (cold-pool only — see
        # assign_pool_account_to_machine's docstring) accounts for the
        # dashboard's "assign to machine" picker.
        unassigned_accounts = conn.execute(
            "SELECT id, email_address FROM otp_email_pool "
            "WHERE reserved_for IS NULL AND assigned_machine_id IS NULL AND enabled "
            "ORDER BY email_address"
        ).fetchall()
        assigned_accounts = conn.execute(
            "SELECT id, email_address, assigned_machine_id FROM otp_email_pool "
            "WHERE reserved_for IS NULL AND assigned_machine_id IS NOT NULL "
            "ORDER BY email_address"
        ).fetchall()
    finally:
        conn.close()
    return jsonify({
        "machines": machines,
        "unassigned_accounts": unassigned_accounts,
        "assigned_accounts": assigned_accounts,
        "coordinator_enabled": config.FLEET_COORDINATOR_ENABLED,
        "plan_hour": config.FLEET_PLAN_HOUR,
        "per_account_daily_cap": config.FLEET_PER_ACCOUNT_DAILY_CAP,
    })


@app.route("/api/fleet-status/accounts/<int:account_id>/assign", methods=["POST"])
def fleet_assign_account(account_id):
    denied = require_admin()
    if denied:
        return denied
    body = request.json or {}
    machine_id = body.get("machine_id")  # None/null unassigns
    if machine_id is not None and not isinstance(machine_id, int):
        return jsonify({"error": "machine_id must be an integer or null."}), 400
    conn = db.get_connection()
    try:
        ok = db.assign_pool_account_to_machine(conn, account_id, machine_id)
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "Account not found."}), 404
    return jsonify({"success": True})


@app.route("/api/fleet-status/machines/<int:machine_id>/status", methods=["POST"])
def fleet_set_machine_status(machine_id):
    denied = require_admin()
    if denied:
        return denied
    body = request.json or {}
    status = (body.get("status") or "").strip()
    conn = db.get_connection()
    try:
        try:
            ok = db.set_fleet_machine_status(conn, machine_id, status)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "Machine not found."}), 404
    return jsonify({"success": True})


@app.route("/api/fleet-status/machines/<int:machine_id>/capacity", methods=["POST"])
def fleet_set_machine_capacity(machine_id):
    denied = require_admin()
    if denied:
        return denied
    body = request.json or {}
    capacity_hint = body.get("capacity_hint")  # None clears the override
    if capacity_hint is not None and not isinstance(capacity_hint, int):
        return jsonify({"error": "capacity_hint must be an integer or null."}), 400
    conn = db.get_connection()
    try:
        ok = db.set_fleet_machine_capacity_hint(conn, machine_id, capacity_hint)
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "Machine not found."}), 404
    return jsonify({"success": True})


@app.route("/api/fleet-status/replan", methods=["POST"])
def fleet_replan():
    """Manually invoke the same planning function the daily tick calls
    (see fleet_scheduler.py) — for when a machine drops mid-day or an
    admin just reassigned accounts and wants today's plan updated right
    away rather than waiting for the next scheduled tick."""
    denied = require_admin()
    if denied:
        return denied
    conn = db.get_connection()
    try:
        result = db.plan_daily_fleet_assignments(
            conn, datetime.date.today(),
            per_account_daily_cap=config.FLEET_PER_ACCOUNT_DAILY_CAP,
            heartbeat_stale_seconds=config.FLEET_HEARTBEAT_STALE_SECONDS,
        )
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/fleet/claim-more", methods=["POST"])
def fleet_claim_more():
    """Machine-to-machine endpoint (called by the local scraper's own
    _run_batch_job, with ops Basic-auth — not a browser call) — lets an
    already-authenticated batch session keep going back-to-back on the SAME
    login instead of logging out every fixed N marks, mirroring how Auto
    Discover holds one session until it dies rather than a fixed batch size.
    Just a thin HTTP wrapper around claim_fleet_assigned_batch, called
    repeatedly by the scraper as long as its current session stays alive."""
    denied = require_admin()
    if denied:
        return denied
    body = request.json or {}
    machine_id = body.get("machine_id")
    batch_size = body.get("batch_size", 15)
    if not isinstance(machine_id, int):
        return jsonify({"error": "machine_id (int) is required."}), 400
    if not isinstance(batch_size, int) or batch_size <= 0:
        return jsonify({"error": "batch_size must be a positive integer."}), 400
    conn = db.get_connection()
    try:
        rows = db.claim_fleet_assigned_batch(conn, machine_id, batch_size)
    finally:
        conn.close()
    return jsonify({"app_nos": [r["app_no"] for r in rows]})


@app.route("/api/fleet/report-result", methods=["POST"])
def fleet_report_result():
    """Companion to /api/fleet/claim-more — the scraper's _run_batch_job
    calls this for every item it claimed THAT WAY (not the original batch,
    which the normal poll loop in scheduler.py's _run_batch already
    applies via _apply_batch_result). Without this, a refilled item would
    sit at status='fetching' forever (until the separate stale-fetching
    reaper resets it), since nothing else ever reports its outcome back —
    the poll loop only knows about the app_nos it originally sent.
    Mirrors _apply_batch_result's own hit/miss/error handling exactly,
    just looked up by app_no + machine_id instead of a pre-fetched dict,
    since the scraper only knows the app_no here."""
    denied = require_admin()
    if denied:
        return denied
    body = request.json or {}
    machine_id = body.get("machine_id")
    app_no = str(body.get("app_no") or "").strip()
    outcome = body.get("outcome")
    search_id = body.get("search_id")
    message = body.get("message") or ""
    if not isinstance(machine_id, int) or not app_no or outcome not in ("hit", "miss", "error"):
        return jsonify({"error": "machine_id (int), app_no, and outcome (hit/miss/error) are required."}), 400

    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT t.id FROM my_trademarks t
            JOIN fleet_assignments fa ON fa.my_trademark_id = t.id
            WHERE fa.machine_id = %(machine_id)s AND fa.work_date = current_date
              AND fa.status = 'claimed' AND t.app_no = %(app_no)s
            """,
            {"machine_id": machine_id, "app_no": app_no},
        ).fetchone()
        if row is None:
            # Nothing to apply this to (already reported, reaped as stale,
            # or claimed by a different machine somehow) — not an error the
            # caller needs to retry over.
            return jsonify({"applied": False})
        tm_id = row["id"]

        if outcome == "hit":
            if search_id and db.has_required_trademark_fields(conn, app_no, search_id):
                db.mark_trademark_fetched(conn, tm_id, search_id)
                scheduler._report_fleet_result(conn, tm_id, "done")
            else:
                scheduler._mark_trademark_error(
                    conn, tm_id, app_no,
                    "Portal returned a partial record (missing key fields) — will retry.",
                    retryable=True,
                )
                scheduler._report_fleet_result(conn, tm_id, "failed")
        elif outcome == "miss":
            scheduler._mark_trademark_error(conn, tm_id, app_no, message or "No results found.", retryable=False)
            scheduler._report_fleet_result(conn, tm_id, "failed")
        else:  # "error"
            scheduler._mark_trademark_error(
                conn, tm_id, app_no, message or "Batch scrape failed.",
                retryable=scheduler._is_retryable(message),
            )
            scheduler._report_fleet_result(conn, tm_id, "failed")
    finally:
        conn.close()
    return jsonify({"applied": True})


@app.route("/retry-all-errors", methods=["POST"])
def retry_all_errors():
    """Bulk 'Refetch' for every row currently in 'error' — mainly needed
    after a shared-resource outage (OTP pool exhausted, scraper app briefly
    unreachable) pushes many rows past the auto-retry cap around the same
    time, each then stuck waiting on an individual manual click. Admin-only
    — this touches every tenant's error rows, not just the caller's own."""
    denied = require_admin()
    if denied:
        return denied
    conn = db.get_connection()
    try:
        if config.FLEET_CONTROL_ENABLED:
            rows = conn.execute("UPDATE my_trademarks SET status='pending',last_error=NULL,updated_at=now() WHERE status='error' RETURNING app_no,user_id").fetchall()
            for row in rows:
                db.queue_fetch_request(conn, row['app_no'], row['user_id'])
            conn.commit()
            count = len(rows)
        else:
            count = db.retry_all_errors(conn)
    finally:
        conn.close()
    return redirect(url_for("trademarks_list", retried=count))


@app.route("/trademark/<int:id_>/remove", methods=["POST"])
def trademark_remove(id_):
    conn = db.get_connection()
    try:
        ok = db.remove_trademark(conn, id_, effective_user_filter())
    finally:
        conn.close()
    if not ok:
        abort(404)
    return redirect(url_for("trademarks_list"))


if __name__ == "__main__":
    if not config.FLEET_CONTROL_ENABLED:
        _startup_conn = db.get_connection()
        try:
            cleared = db.release_stuck_fetching_trademarks(_startup_conn)
            if cleared:
                logging.info("Reset %d trademark(s) stuck in 'fetching' from a previous run.", cleared)
        finally:
            _startup_conn.close()
    scheduler.start()
    if not config.FLEET_CONTROL_ENABLED:
        fleet_scheduler.start()

    # Self-healing Journal Watch recovery — see parse_journal.recompute_all_matches's
    # docstring for the 2026-09-21 incident this fixes: a code deploy's
    # service restart used to be able to kill journal similarity-matching
    # mid-run and silently leave journal_matches empty for that journal,
    # with no way to notice short of an admin re-running it by hand over
    # SSH. Now it just re-derives every journal's matches fresh on every
    # startup (cheap and idempotent — see that function's docstring), in a
    # background thread so it never delays this process binding its port.
    def _recover_journal_matches():
        try:
            results = parse_journal.recompute_all_matches()
            logging.info("Journal Watch: recomputed matches for %d journal(s) at startup.", len(results))
        except Exception:
            logging.exception("Journal Watch: startup match recovery failed")

    if config.BACKGROUND_TASKS_ENABLED:
        threading.Thread(target=_recover_journal_matches, name="journal-match-recovery", daemon=True).start()

    # Auth: real per-user login against the users table (see _require_login
    # above) — always on now that real accounts exist.
    logging.info("TM Manager auth: per-user login enabled.")

    # NOTE: bound to 0.0.0.0 — reachable from other devices on the local
    # network. Without the auth gate above, no authentication — trusted
    # private network only.
    if getattr(config, "USE_WAITRESS", False):
        from waitress import serve
        logging.info("Serving TM Manager with waitress on %s:%s", config.TM_BIND_HOST, config.TM_MANAGER_PORT)
        serve(app, host=config.TM_BIND_HOST, port=config.TM_MANAGER_PORT, threads=8)
    else:
        # debug=False for the reloader-free, safer dev default (was True — an
        # RCE risk if ever reachable off the local box).
        app.run(host=config.TM_BIND_HOST, port=config.TM_MANAGER_PORT, debug=False, threaded=True)
