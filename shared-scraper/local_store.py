"""
Local-first scraping store — SQLite file this machine can read/write with
ZERO network calls to Contabo, used while a VPN tunnel is active for IP
India access (see vpn_rotator.py). Replaces the earlier
try-Postgres-then-fall-back-to-a-JSON-marker approach (pending_sync.py,
offline_queue.py) for the specific case this module exists for: the
EVERY-call-touches-Contabo pattern throughout app.py's scraping functions
(claim_free_pool_account, release_pool_account, save_ipindia_search,
update_series_progress, etc.) each independently retrying a 30-second
Postgres timeout and, in aggregate, stalling or killing a whole scrape job
— see the conversation this was built from for the specific incident
(series job b34ed6c31fae459a9670dc792ed4d100, 2026-10-01, died after
repeated PoolTimeouts while VPN was connected).

Design: while LOCAL_MODE is on, the scraping code path below does not
call db.get_connection() AT ALL — every read/write goes to this SQLite
file instead, which never times out and never depends on network
reachability. A separate sync pass (sync_to_contabo, called by
offline_queue's existing background loop once Postgres is reachable)
pushes everything collected locally into the real Postgres tables, then
clears the local rows — so Contabo ends up with exactly the same data it
would have gotten if it had been reachable the whole time, just later.

Pool accounts: mirrored from otp_email_pool into pool_accounts_cache
whenever Contabo IS reachable (refresh_pool_cache, called right before
turning local mode on) — claiming/releasing/cooldown during a local-mode
run reads and writes ONLY this local copy. Changes made locally (which
account is claimed, cooldown timestamps) are NOT written back to
Postgres — Postgres's own copy is the source of truth and gets a fresh
refresh_pool_cache() next time local mode starts, so a stale local claim
never permanently diverges from reality once Contabo is reachable again.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from folder_export import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "local_store.sqlite3"
POOL_ACCOUNT_COOLDOWN_SECONDS = 120  # mirrors db.py's _POOL_ACCOUNT_COOLDOWN_SECONDS

_lock = threading.Lock()
LOCAL_MODE = False  # flip via set_local_mode(); scraping code checks this, not a direct import cycle


def set_local_mode(on: bool) -> None:
    global LOCAL_MODE
    LOCAL_MODE = on
    log.info("local_store: local mode %s.", "ENABLED (VPN active)" if on else "disabled")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pool_accounts_cache (
            id INTEGER PRIMARY KEY,
            email_address TEXT NOT NULL,
            app_password TEXT NOT NULL,
            imap_server TEXT NOT NULL,
            imap_port INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            claimed_by_job_id TEXT,
            claimed_at REAL,
            last_used_at REAL
        );

        CREATE TABLE IF NOT EXISTS local_hits (
            bundle_id TEXT PRIMARY KEY,
            queued_at REAL NOT NULL,
            payload_json TEXT NOT NULL,
            synced INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS local_series_progress (
            series TEXT PRIMARY KEY,
            last_checked_number INTEGER,
            hits_count INTEGER NOT NULL DEFAULT 0,
            misses_count INTEGER NOT NULL DEFAULT 0,
            errors_count INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );
    """)
    conn.commit()


def refresh_pool_cache(accounts: list[dict]) -> None:
    """Call with db.list_pool_accounts(conn)'s result while Contabo IS
    reachable, right before enabling local mode — replaces the entire
    local cache with a fresh snapshot so stale claims/cooldowns from a
    previous local-mode run never leak into this one."""
    with _lock, _connect() as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM pool_accounts_cache")
        conn.executemany(
            """
            INSERT INTO pool_accounts_cache
                (id, email_address, app_password, imap_server, imap_port, enabled, claimed_by_job_id, claimed_at, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            """,
            [
                (a["id"], a["email_address"], a["app_password"], a["imap_server"], a["imap_port"],
                 1 if a["enabled"] else 0)
                for a in accounts
            ],
        )
        conn.commit()
    log.info("local_store: pool cache refreshed with %d account(s).", len(accounts))


def claim_free_pool_account_local(job_id: str) -> dict | None:
    """Local-only equivalent of db.claim_free_pool_account — same
    round-robin-by-least-recently-used, same cooldown, zero network."""
    now = time.time()
    with _lock, _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT * FROM pool_accounts_cache
            WHERE enabled = 1 AND claimed_by_job_id IS NULL
              AND (last_used_at IS NULL OR last_used_at <= ?)
            ORDER BY last_used_at IS NOT NULL, last_used_at ASC, id ASC
            LIMIT 1
            """,
            (now - POOL_ACCOUNT_COOLDOWN_SECONDS,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE pool_accounts_cache SET claimed_by_job_id = ?, claimed_at = ?, last_used_at = ? WHERE id = ?",
            (job_id, now, now, row["id"]),
        )
        conn.commit()
        return dict(row)


def release_pool_account_local(account_id: int) -> None:
    with _lock, _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE pool_accounts_cache SET claimed_by_job_id = NULL WHERE id = ?",
            (account_id,),
        )
        conn.commit()


def release_all_claims_local() -> None:
    """Call at the start of a local-mode session (mirrors
    db.release_all_pool_claims) so no claim survives across runs."""
    with _lock, _connect() as conn:
        _ensure_schema(conn)
        conn.execute("UPDATE pool_accounts_cache SET claimed_by_job_id = NULL")
        conn.commit()


def save_local_hit(bundle: dict) -> None:
    """Store one full hit bundle (same shape as offline_queue.new_bundle's
    output — search + documents + reminders + correspondence + page
    fetches + image) with zero network calls. sync_to_contabo later
    replays these exactly like offline_queue replays its own bundles."""
    with _lock, _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO local_hits (bundle_id, queued_at, payload_json, synced) VALUES (?, ?, ?, 0)",
            (bundle["bundle_id"], bundle["queued_at"], json.dumps(bundle, ensure_ascii=False)),
        )
        conn.commit()
    log.info("local_store: saved hit for app_no=%s locally (zero network calls).", bundle["search"]["app_no"])


def update_series_progress_local(series: str, last_checked_number: int,
                                  hits_count: int, misses_count: int, errors_count: int) -> None:
    now = time.time()
    with _lock, _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO local_series_progress (series, last_checked_number, hits_count, misses_count, errors_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(series) DO UPDATE SET
                last_checked_number = excluded.last_checked_number,
                hits_count = excluded.hits_count,
                misses_count = excluded.misses_count,
                errors_count = excluded.errors_count,
                updated_at = excluded.updated_at
            """,
            (series, last_checked_number, hits_count, misses_count, errors_count, now),
        )
        conn.commit()


def get_unsynced_hits() -> list[dict]:
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute("SELECT bundle_id, payload_json FROM local_hits WHERE synced = 0").fetchall()
        return [json.loads(r["payload_json"]) for r in rows]


def mark_hit_synced(bundle_id: str) -> None:
    with _lock, _connect() as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM local_hits WHERE bundle_id = ?", (bundle_id,))
        conn.commit()


def pending_hit_count() -> int:
    with _connect() as conn:
        _ensure_schema(conn)
        return conn.execute("SELECT count(*) AS n FROM local_hits WHERE synced = 0").fetchone()["n"]
