"""
PostgreSQL storage for TM Manager's own tracking data.

Shares the SAME "datascraper" database the scraper app uses, but owns just
one new table (my_trademarks) - it never writes to the scraper's own tables
directly; it only reads ipindia_results/tma_documents indirectly by storing
a pointer (last_search_id) once the scraper app confirms a fetch succeeded.
"""

import html as _html
import re
import threading
import uuid
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import config

USERS_SCHEMA = """
-- One row per signed-up TM Manager account. is_admin=true gets an
-- unfiltered view across every user's trademarks (see app.py's
-- effective_user_filter) - everyone else only ever sees their own.
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email          TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    is_admin       BOOLEAN NOT NULL DEFAULT false,
    display_name   TEXT,
    is_active      BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email ON users(lower(email));
-- Tracks the last time this user's daily status-change digest email was
-- sent — lets the digest job compute "what changed since last time" per
-- user and guarantees a restart never sends a duplicate or skips a day
-- (see scheduler._digest_loop).
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_digest_sent_at TIMESTAMPTZ;
-- Personal Trademarks-table column choice + order, e.g. {"columns":
-- ["word_mark","class_number","portal_status"]} — App Number is never
-- stored here, it's always shown first and isn't configurable. NULL means
-- "this user hasn't customized it yet, use the app's default set."
ALTER TABLE users ADD COLUMN IF NOT EXISTS trademarks_column_prefs JSONB;
"""

MY_TRADEMARKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS my_trademarks (
    id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    app_no            TEXT NOT NULL,
    label             TEXT,
    notes             TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    job_id            TEXT,
    last_error        TEXT,
    last_search_id    INTEGER,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    last_attempted_at TIMESTAMPTZ,
    fetched_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_my_trademarks_status ON my_trademarks(status);
-- priority: higher number = fetched sooner. A trademark you add through the
-- TM Manager UI gets priority 100; a bulk/migration/backfill add gets 0.
ALTER TABLE my_trademarks ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_my_trademarks_queue
    ON my_trademarks (status, priority DESC, created_at DESC);
-- Multi-tenant, step 1 of 2 (safe to run unconditionally, even against a
-- database that still has pre-multi-tenant rows): add the column nullable.
-- Step 2 (making it NOT NULL and swapping the unique constraint) is a
-- SEPARATE statement — see lock_my_trademarks_to_users() below — run only
-- by scripts/create_admin_and_backfill.py, AFTER it confirms every row has
-- been assigned an owner. Doing the NOT NULL/constraint swap here instead
-- would make _ensure_schema (which runs on every single app startup, via
-- the very first get_connection() call) crash immediately on a database
-- with any pre-existing NULL user_id row, since nothing would have had a
-- chance to backfill them first — SET NOT NULL is validated instantly
-- against current data, not deferred until some later point.
ALTER TABLE my_trademarks ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_my_trademarks_user ON my_trademarks(user_id);
-- kind distinguishes what a tracked app_no MEANS to this user, not how it's
-- fetched — every kind goes through the exact same auto-fetch pipeline
-- (scheduler._worker_loop, fast_fetch_new_trademarks, staleness refresh),
-- so an Outbound Opposition target gets the same daily status refresh and
-- change history as a portfolio mark. 'portfolio' = a mark this user owns/
-- manages (the original, default meaning of every pre-existing row).
-- 'opposition_out' = someone else's application this user's team is
-- opposing — tracked here purely to watch that application's own live
-- status, since India's registry has no separate "opposition proceeding"
-- record to scrape. Swapping the unique index to include kind lets the
-- same app_no be tracked under both kinds independently (e.g. you own
-- 1234567 AND separately track 7654321 as an opposition target).
ALTER TABLE my_trademarks ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'portfolio';
DROP INDEX IF EXISTS uq_my_trademarks_user_app;
CREATE UNIQUE INDEX IF NOT EXISTS uq_my_trademarks_user_app_kind ON my_trademarks(user_id, app_no, kind);
CREATE INDEX IF NOT EXISTS idx_my_trademarks_kind ON my_trademarks(kind);
-- Google Drive migration: caches the Drive folder id for this app_no so
-- an upload doesn't need a Drive lookup-or-create call every time — one
-- create call per app_no instead of one per document. Since app_no isn't
-- unique on this table (the same app_no can have multiple rows — one per
-- (user_id, kind) combination), this value is naturally duplicated across
-- those rows for the same app_no; that's harmless redundancy (all of them
-- point at the same real Drive folder), not a correctness issue.
ALTER TABLE my_trademarks ADD COLUMN IF NOT EXISTS drive_folder_id TEXT;
"""

_LOCK_MY_TRADEMARKS_TO_USERS_SQL = """
DROP INDEX IF EXISTS uq_my_trademarks_app_no;
ALTER TABLE my_trademarks ALTER COLUMN user_id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_my_trademarks_user_app ON my_trademarks(user_id, app_no);
"""


def lock_my_trademarks_to_users(conn: psycopg.Connection) -> None:
    """Multi-tenant, step 2 of 2 — makes user_id required and swaps the
    unique constraint from app_no alone to (user_id, app_no). Call this
    ONLY after every my_trademarks row has a real user_id (see
    scripts/create_admin_and_backfill.py, which checks this and calls here
    once the count of NULL rows is confirmed zero). Idempotent — safe to
    call again on a database where this has already run."""
    conn.execute(_LOCK_MY_TRADEMARKS_TO_USERS_SQL)
    conn.commit()

JOURNAL_ENTRIES_SCHEMA = """
-- One row per trademark entry parsed out of a Trade Marks Journal PDF -
-- populated once per journal by scripts/parse_journal.py, never edited by
-- the running app. journal_no + app_no is unique because the same
-- application can legitimately reappear in a later journal (e.g. after a
-- correction), and each reappearance is worth keeping as its own row.
CREATE TABLE IF NOT EXISTS journal_entries (
    id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    journal_no     TEXT NOT NULL,
    journal_date   TEXT,
    app_no         TEXT NOT NULL,
    app_date       TEXT,
    class_number   TEXT,
    word_mark      TEXT,
    applicant_name TEXT,
    applicant_address TEXT,
    page_number    INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A journal split across several class-range PDFs (Class 1-9, 10-25, ...)
-- has page_number reset to 1 in each file, so knowing which page an entry
-- came from also requires knowing which file — added as a nullable ALTER
-- (rather than in the CREATE above) so it applies cleanly to a database
-- that already has this table from before multi-file uploads existed.
ALTER TABLE journal_entries ADD COLUMN IF NOT EXISTS source_file TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_entries_journal_app ON journal_entries(journal_no, app_no);
CREATE INDEX IF NOT EXISTS idx_journal_entries_journal_no ON journal_entries(journal_no);
CREATE INDEX IF NOT EXISTS idx_journal_entries_word_mark ON journal_entries(word_mark);
"""

JOURNAL_MATCHES_SCHEMA = """
-- Precomputed similarity scores between a portfolio trademark and a journal
-- entry - computing difflib ratios across ~200 portfolio marks x ~1800
-- journal entries on every page load would be too slow, so
-- scripts/parse_journal.py scores and stores only pairs above a minimum
-- threshold once, and this table is just read from at request time.
CREATE TABLE IF NOT EXISTS journal_matches (
    id                 INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    my_trademark_id    INTEGER NOT NULL REFERENCES my_trademarks(id) ON DELETE CASCADE,
    journal_entry_id    INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    similarity         REAL NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_matches_pair ON journal_matches(my_trademark_id, journal_entry_id);
CREATE INDEX IF NOT EXISTS idx_journal_matches_tm ON journal_matches(my_trademark_id);
CREATE INDEX IF NOT EXISTS idx_journal_matches_similarity ON journal_matches(similarity DESC);
"""

JOURNAL_JOBS_SCHEMA = """
-- Tracks one upload-and-parse run — possibly several PDFs at once (IP
-- India splits each week's journal across multiple class-range files,
-- e.g. Class 1-9, 10-25, 26-34, all published together under one journal
-- number/date), so the frontend can poll one job for progress across the
-- whole batch instead of holding the upload request open for the
-- several minutes a full journal's worth of pages takes to parse (see
-- scripts/parse_journal.py, which is what actually does the work and
-- updates this row's progress fields as it goes).
CREATE TABLE IF NOT EXISTS journal_jobs (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    journal_no       TEXT,
    journal_date     TEXT,
    filename         TEXT NOT NULL,
    total_files      INTEGER NOT NULL DEFAULT 1,
    files_done       INTEGER NOT NULL DEFAULT 0,
    current_filename TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    total_pages      INTEGER,
    pages_done       INTEGER NOT NULL DEFAULT 0,
    entries_found    INTEGER NOT NULL DEFAULT 0,
    matches_found    INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Added after the initial table existed on some databases (multi-file
-- upload support) — ALTERs rather than part of the CREATE above so they
-- apply cleanly to a database that already has this table.
ALTER TABLE journal_jobs ADD COLUMN IF NOT EXISTS journal_date TEXT;
ALTER TABLE journal_jobs ADD COLUMN IF NOT EXISTS total_files INTEGER NOT NULL DEFAULT 1;
ALTER TABLE journal_jobs ADD COLUMN IF NOT EXISTS files_done INTEGER NOT NULL DEFAULT 0;
ALTER TABLE journal_jobs ADD COLUMN IF NOT EXISTS current_filename TEXT;
"""

SEARCH_HISTORY_SCHEMA = """
-- One row per TM Search lookup a user performs, recorded here (not in the
-- scraper's own shared ipindia_results table) specifically so search
-- history can be per-user without touching the scraper's schema/code — the
-- scraper's tables stay global/shared on purpose (see get_trademark_data),
-- but WHO searched something is TM Manager's own concern.
CREATE TABLE IF NOT EXISTS tm_search_history (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    app_no      TEXT NOT NULL,
    search_id   INTEGER,   -- points into the scraper's ipindia_results.id, best-effort
    searched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tm_search_history_user ON tm_search_history(user_id, searched_at DESC);
"""

TM_STATUS_HISTORY_SCHEMA = """
-- Append-only ledger: one row per successful fetch of a portfolio mark,
-- capturing a full snapshot of the fields we track PLUS a per-field diff
-- vs. the previous snapshot. Never overwritten. This is the source of the
-- "previous status vs current status, with dates" timeline on the detail
-- page and the portfolio-wide change feed.
CREATE TABLE IF NOT EXISTS tm_status_history (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    my_trademark_id     INTEGER NOT NULL REFERENCES my_trademarks(id) ON DELETE CASCADE,
    app_no              TEXT NOT NULL,
    search_id           INTEGER,              -- the ipindia_results row this came from
    fetched_at          TIMESTAMPTZ NOT NULL,
    status              TEXT,
    sub_status          TEXT,
    trade_mark          TEXT,
    proprietor_name     TEXT,
    class_number        TEXT,
    valid_upto          TEXT,
    publication_details TEXT,
    tm_type             TEXT,
    changed_fields      JSONB,                -- {"status": {"from": "...", "to": "..."}, ...}
    is_first            BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tsh_tm ON tm_status_history(my_trademark_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_tsh_fetched ON tm_status_history(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_tsh_changed ON tm_status_history(fetched_at DESC)
  WHERE changed_fields IS NOT NULL AND changed_fields <> '{}'::jsonb;
CREATE UNIQUE INDEX IF NOT EXISTS uq_tsh_tm_search ON tm_status_history(my_trademark_id, search_id)
  WHERE search_id IS NOT NULL;
"""

CUSTOM_TASKS_SCHEMA = """
-- User-created to-dos on the Tasks & Compliances page, shown alongside
-- (but never confused with) the read-only reminder notices IP India
-- publishes. Brand new table (no pre-existing rows), so user_id is
-- NOT NULL from day one - unlike my_trademarks.user_id, there's no
-- legacy nullable-then-lock migration to replay here.
CREATE TABLE IF NOT EXISTS custom_tasks (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    title               TEXT NOT NULL,
    due_date            DATE NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    my_trademark_id     INTEGER REFERENCES my_trademarks(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_custom_tasks_user ON custom_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_custom_tasks_tm ON custom_tasks(my_trademark_id);
CREATE INDEX IF NOT EXISTS idx_custom_tasks_due ON custom_tasks(due_date);
"""

CUSTOM_TASK_STATUSES = ("pending", "in_progress", "completed", "closed")

COMPLIANCE_STATUS_SCHEMA = """
-- Per-user status tracking for a read-only registry notice (a reminder or
-- correspondence row the scraper wrote to ITS OWN ipindia_reminders /
-- ipindia_correspondence tables — never touched or altered here). Those
-- tables have no status column of their own and never will (they're a
-- verbatim record of what the portal published), so a user's "I've replied
-- to this" / "marked complete" click has nowhere to live except a separate
-- table tm_manager owns, keyed back to that row's real id + which table it
-- came from. source is 'reminder' or 'correspondence' (matches
-- list_reminders/list_correspondence's source table); source_id is that
-- row's own `id` column in the scraper's database. One status per
-- (source, source_id, user_id) — two different tenants tracking the same
-- portfolio's reminder independently, same multi-tenant shape as
-- custom_tasks.
CREATE TABLE IF NOT EXISTS compliance_status (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source      TEXT NOT NULL,
    source_id   INTEGER NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    status      TEXT NOT NULL DEFAULT 'pending',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_compliance_status_source_user
    ON compliance_status(source, source_id, user_id);
"""

FLEET_SCHEMA = """
-- Phase 0 of the Contabo-coordinator + local-worker-fleet migration (see
-- the plan doc for the full design). Schema only in this phase — nothing
-- reads or writes these tables yet, and FLEET_MODE_ENABLED/
-- FLEET_COORDINATOR_ENABLED both default off, so this is a zero-behavior-
-- change addition for every existing single-machine install.
--
-- fleet_machines: the registry of local worker machines. "Online" vs
-- "offline" is derived at READ time from last_heartbeat_at age (see
-- FLEET_HEARTBEAT_STALE_SECONDS), never stored, so there's only one source
-- of truth for liveness. status is admin-controlled (active/disabled),
-- independent of whether a machine is currently heartbeating.
CREATE TABLE IF NOT EXISTS fleet_machines (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    machine_key         TEXT UNIQUE NOT NULL,
    display_name        TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
    capacity_hint       INTEGER,
    last_heartbeat_at   TIMESTAMPTZ,
    last_heartbeat_meta JSONB,
    registered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fleet_machines_status ON fleet_machines(status);

-- fleet_assignments: which my_trademarks rows are assigned to which worker
-- machine for a given work_date. This table is what makes daily-batch
-- claiming machine-scoped WITHOUT touching the existing global claim
-- queries (claim_batch_pending_trademarks stays completely untouched — see
-- the planned claim_fleet_assigned_batch sibling). status tracks the
-- ASSIGNMENT lifecycle (assigned/claimed/done/failed), independent of
-- my_trademarks.status which tracks the FETCH lifecycle — a row can be
-- 'assigned' here and still 'pending' there until a worker actually claims
-- it locally. The partial unique index guarantees one TM is never
-- double-assigned to two machines on the same day; it deliberately does
-- NOT include work_date, so a still-open (assigned/claimed) row from a
-- past day blocks a fresh assignment for that TM until it resolves —
-- this is what implements "a machine that doesn't finish stays pinned to
-- it, retried the next day" rather than being freely reshuffled.
CREATE TABLE IF NOT EXISTS fleet_assignments (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    work_date        DATE NOT NULL,
    machine_id       INTEGER NOT NULL REFERENCES fleet_machines(id) ON DELETE CASCADE,
    my_trademark_id  INTEGER NOT NULL REFERENCES my_trademarks(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'assigned',
    assigned_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at       TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_fleet_assignments_active_tm
    ON fleet_assignments (my_trademark_id) WHERE status IN ('assigned', 'claimed');
CREATE INDEX IF NOT EXISTS idx_fleet_assignments_machine_date ON fleet_assignments(machine_id, work_date, status);
CREATE INDEX IF NOT EXISTS idx_fleet_assignments_work_date ON fleet_assignments(work_date);
"""

FLEET_ASSIGNMENT_STATUSES = ("assigned", "claimed", "done", "failed")

BILLING_SCHEMA = """
-- Subscription/billing system. A "plan" (Starter/Pro/Growth/Max/Custom) sets
-- default limits + features; a "subscription" attaches one plan to one
-- PAYING user and can override any of those defaults per-account (e.g. bump
-- one Starter customer from 250 to 300 TMs without touching the Starter
-- plan itself, which would affect every other Starter customer too).
--
-- No payment processor integration here by design (deferred) — an admin
-- creates/edits subscriptions by hand via the admin UI; a webhook handler
-- can call the exact same billing.py functions later without any schema
-- change.
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          TEXT NOT NULL,
    price_cents   INTEGER,
    tm_limit      INTEGER NOT NULL,
    seat_limit    INTEGER NOT NULL,
    features      JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active     BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per PAYING account (user_id — never a seat's own id; seats are
-- users.parent_account_id pointing at the payer, see below). UNIQUE on
-- user_id: a paying account has at most one subscription at a time — an
-- upgrade/downgrade updates this row rather than inserting a second one, so
-- "this account's current plan" is always a single unambiguous lookup.
-- renews_at NULL means "no expiry tracked" (e.g. an admin-comped account);
-- billing.py's grace-period logic only applies when renews_at is set.
CREATE TABLE IF NOT EXISTS subscriptions (
    id                   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id              INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    plan_id              INTEGER NOT NULL REFERENCES plans(id),
    status               TEXT NOT NULL DEFAULT 'active',
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    renews_at            TIMESTAMPTZ,
    tm_limit_override    INTEGER,
    seat_limit_override  INTEGER,
    feature_overrides    JSONB,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);

-- A seat is just a users row pointing at the paying account that owns it —
-- no separate membership table. Keeps every existing ownership check in
-- this file (WHERE user_id = ...) unchanged; billing.py resolves the PAYING
-- account by following parent_account_id when it's set. NULL (the default)
-- means "this row IS a paying account, not a seat under one".
ALTER TABLE users ADD COLUMN IF NOT EXISTS parent_account_id INTEGER REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_users_parent_account ON users(parent_account_id) WHERE parent_account_id IS NOT NULL;

-- TM Search quota, same override pattern as tm_limit/tm_limit_override.
-- search_limit NULL = unlimited (same "no cap" meaning tm_limit's NULL
-- never has, since tm_limit is NOT NULL — kept nullable here on purpose so
-- a plan can omit a search cap entirely without needing a huge sentinel
-- number). billing_period_months sets how often the count resets (1/3/6/12
-- etc, admin's choice per plan) — see billing.py's _current_period_start
-- for how that boundary is computed from a subscription's started_at/
-- renews_at.
ALTER TABLE plans ADD COLUMN IF NOT EXISTS search_limit INTEGER;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS billing_period_months INTEGER NOT NULL DEFAULT 1;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS search_limit_override INTEGER;
"""

SUBSCRIPTION_STATUSES = ("active", "expired", "cancelled")
SUBSCRIPTION_GRACE_DAYS = 3

_schema_ready = False
_schema_lock = threading.Lock()

# ── Connection pool (see the scraper's db.py for the rationale) ─────────────
_POOL = None
_pool_lock = threading.Lock()


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn.execute(USERS_SCHEMA)
        conn.execute(MY_TRADEMARKS_SCHEMA)
        conn.execute(JOURNAL_ENTRIES_SCHEMA)
        conn.execute(JOURNAL_MATCHES_SCHEMA)
        conn.execute(JOURNAL_JOBS_SCHEMA)
        conn.execute(SEARCH_HISTORY_SCHEMA)
        conn.execute(TM_STATUS_HISTORY_SCHEMA)
        conn.execute(CUSTOM_TASKS_SCHEMA)
        conn.execute(COMPLIANCE_STATUS_SCHEMA)
        conn.execute(FLEET_SCHEMA)
        conn.execute(BILLING_SCHEMA)
        conn.commit()
        _schema_ready = True


def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    with _pool_lock:
        if _POOL is not None:
            return _POOL
        from psycopg_pool import ConnectionPool
        conninfo = (
            f"host={config.PG_HOST} port={config.PG_PORT} "
            f"dbname={config.PG_DBNAME} user={config.PG_USER}"
        )
        if config.PG_SSLCERT and config.PG_SSLKEY:
            # Client-cert auth — see the root db.py's identical block for
            # why (VPN/IP-independent auth via pg_hba.conf's "cert" rule).
            conninfo += f" sslmode=require sslcert={config.PG_SSLCERT} sslkey={config.PG_SSLKEY}"
        elif config.PG_PASSWORD:
            conninfo += f" password={config.PG_PASSWORD}"
        if config.TM_DATABASE_URL:
            conninfo = config.TM_DATABASE_URL
        pool = ConnectionPool(
            conninfo,
            min_size=getattr(config, "DB_POOL_MIN", 2),
            max_size=getattr(config, "DB_POOL_MAX", 10),
            kwargs={"row_factory": dict_row},
            open=True,
            name="tm_manager",
        )
        if config.AUTO_MIGRATE:
            with pool.connection() as c:
                _ensure_schema(c)
        _POOL = pool
        return _POOL


class _PooledConnection:
    __slots__ = ("_pool", "_conn")

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._conn is not None:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
        finally:
            self.close()

    def close(self):
        if self._conn is not None:
            c, self._conn = self._conn, None
            try:
                if not c.closed:
                    c.rollback()
            except Exception:
                pass
            try:
                self._pool.putconn(c)
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass


def get_connection() -> "psycopg.Connection":
    return _PooledConnection(_get_pool())


# ── Fleet coordinator: machine registry, heartbeats, daily assignment ───────
# Phase 0/1 of the Contabo-coordinator + local-worker-fleet migration (see
# the plan doc). Nothing in this section is called from anywhere yet in
# this phase except the (read-only, dashboard-facing) list/plan functions —
# no worker machine claims through fleet_assignments until Phase 2 wires up
# claim_fleet_assigned_batch and the /api/fleet/* endpoints.

def upsert_fleet_machine(conn: psycopg.Connection, machine_key: str, display_name: str | None = None) -> dict:
    """Idempotent register — called by a worker's fleet-mode startup (and by
    the fleet dashboard for manual pre-registration). ON CONFLICT only
    updates display_name (when a non-None one is given) and updated_at;
    it deliberately never touches status/capacity_hint here so an admin's
    'disabled' or capacity override on the dashboard survives a worker
    simply re-registering after a restart."""
    cursor = conn.execute(
        """
        INSERT INTO fleet_machines (machine_key, display_name)
        VALUES (%(machine_key)s, %(display_name)s)
        ON CONFLICT (machine_key) DO UPDATE
            SET display_name = COALESCE(EXCLUDED.display_name, fleet_machines.display_name),
                updated_at = now()
        RETURNING *
        """,
        {"machine_key": machine_key, "display_name": display_name},
    )
    row = cursor.fetchone()
    conn.commit()
    return row


def record_fleet_heartbeat(conn: psycopg.Connection, machine_key: str, meta: dict | None = None) -> bool:
    """Called every 30-60s by a fleet-mode worker. Returns False if
    machine_key isn't registered (caller should re-register) rather than
    raising, since a missed heartbeat is routine, not exceptional."""
    cursor = conn.execute(
        """
        UPDATE fleet_machines
        SET last_heartbeat_at = now(), last_heartbeat_meta = %(meta)s, updated_at = now()
        WHERE machine_key = %(machine_key)s
        """,
        {"machine_key": machine_key, "meta": Jsonb(meta) if meta is not None else None},
    )
    conn.commit()
    return cursor.rowcount > 0


def get_fleet_machine(conn: psycopg.Connection, machine_key: str) -> dict | None:
    cursor = conn.execute(
        "SELECT * FROM fleet_machines WHERE machine_key = %(machine_key)s",
        {"machine_key": machine_key},
    )
    return cursor.fetchone()


def list_fleet_machines(conn: psycopg.Connection, heartbeat_stale_seconds: int) -> list[dict]:
    """Every registered machine, each with an is_online boolean computed
    from heartbeat freshness at READ time (see FLEET_SCHEMA's comment on
    why this is never stored) plus its current assigned/enabled account
    count and today's fleet_assignments status breakdown — everything the
    fleet dashboard's machine table needs in one call."""
    cursor = conn.execute(
        """
        SELECT
            m.*,
            (m.last_heartbeat_at IS NOT NULL
                AND m.last_heartbeat_at > now() - make_interval(secs => %(stale)s)) AS is_online,
            (SELECT count(*) FROM otp_email_pool p
                WHERE p.assigned_machine_id = m.id AND p.enabled) AS account_count,
            (SELECT count(*) FROM fleet_assignments fa
                WHERE fa.machine_id = m.id AND fa.work_date = current_date
                  AND fa.status IN ('assigned', 'claimed')) AS today_pending,
            (SELECT count(*) FROM fleet_assignments fa
                WHERE fa.machine_id = m.id AND fa.work_date = current_date
                  AND fa.status = 'done') AS today_done,
            (SELECT count(*) FROM fleet_assignments fa
                WHERE fa.machine_id = m.id AND fa.work_date = current_date
                  AND fa.status = 'failed') AS today_failed
        FROM fleet_machines m
        ORDER BY m.display_name NULLS LAST, m.machine_key
        """,
        {"stale": heartbeat_stale_seconds},
    )
    return cursor.fetchall()


def list_online_fleet_machines(conn: psycopg.Connection, heartbeat_stale_seconds: int) -> list[dict]:
    """Just the active + currently-heartbeating machines — what planning
    actually divides work across. A machine an admin disabled (status !=
    'active') is excluded even if it's still heartbeating, so 'disable' on
    the dashboard takes effect on the very next planning run without
    needing the machine itself to stop or be reachable."""
    return [
        m for m in list_fleet_machines(conn, heartbeat_stale_seconds)
        if m["status"] == "active" and m["is_online"]
    ]


def set_fleet_machine_status(conn: psycopg.Connection, machine_id: int, status: str) -> bool:
    if status not in ("active", "disabled"):
        raise ValueError(f"Invalid fleet machine status: {status!r}")
    cursor = conn.execute(
        "UPDATE fleet_machines SET status = %(status)s, updated_at = now() WHERE id = %(id)s",
        {"status": status, "id": machine_id},
    )
    conn.commit()
    return cursor.rowcount > 0


def set_fleet_machine_capacity_hint(conn: psycopg.Connection, machine_id: int, capacity_hint: int | None) -> bool:
    cursor = conn.execute(
        "UPDATE fleet_machines SET capacity_hint = %(hint)s, updated_at = now() WHERE id = %(id)s",
        {"hint": capacity_hint, "id": machine_id},
    )
    conn.commit()
    return cursor.rowcount > 0


def assign_pool_account_to_machine(conn: psycopg.Connection, account_id: int, machine_id: int | None) -> bool:
    """Rewrites otp_email_pool.assigned_machine_id for one account — the
    dashboard's "assign accounts to machine" control. machine_id=None
    unassigns (back to Contabo's own shared/cold pool). Deliberately does
    NOT touch reserved_for — an account already reserved for warm_pool/
    wordmark_pool stays excluded from planning's weight calculation
    regardless of this column (see plan_daily_fleet_assignments), so this
    function alone can never accidentally hand a live-search account to a
    worker; the dashboard's own account picker additionally filters to
    reserved_for IS NULL up front as a second, UI-level guard."""
    cursor = conn.execute(
        "UPDATE otp_email_pool SET assigned_machine_id = %(machine_id)s, updated_at = now() WHERE id = %(id)s",
        {"machine_id": machine_id, "id": account_id},
    )
    conn.commit()
    return cursor.rowcount > 0


def count_assigned_accounts(conn: psycopg.Connection, machine_id: int) -> int:
    """How many enabled, non-reserved accounts are assigned to this machine
    right now — used by scheduler.py's start() to size AUTO_FETCH_WORKERS
    to the machine's own account count instead of a fixed .env number, so
    adding/removing accounts on the dashboard takes effect on next restart
    without editing every worker's .env by hand."""
    row = conn.execute(
        "SELECT count(*) AS n FROM otp_email_pool WHERE assigned_machine_id = %(machine_id)s AND enabled",
        {"machine_id": machine_id},
    ).fetchone()
    return row["n"] if row else 0


def plan_daily_fleet_assignments(
    conn: psycopg.Connection, work_date, per_account_daily_cap: int, heartbeat_stale_seconds: int,
) -> dict:
    """Computes and writes today's machine<->TM assignment. Safe to call
    more than once for the same work_date (e.g. the dashboard's "Replan
    now" button) - INSERT ... ON CONFLICT DO NOTHING means it only ever
    ADDS newly-assignable rows on top of whatever's already assigned/
    claimed/done, it never un-assigns or reshuffles existing rows (matches
    the "a machine that doesn't finish stays pinned to it" decision - no
    reshuffling happens here, ever, only forward-filling of remaining
    capacity).

    Returns {"machines": n, "total_weight": n, "due": n, "assigned": n} for
    the caller (fleet_scheduler's log line, or the dashboard's replan
    response) to report back - "assigned" is the actual INSERT row count,
    which can be less than "due" if fleet capacity is smaller than the due
    pool (the remainder simply stays unassigned, picked up by tomorrow's
    plan or the legacy global claim path for any machine not in fleet
    mode).
    """
    machines = list_online_fleet_machines(conn, heartbeat_stale_seconds)
    if not machines:
        return {"machines": 0, "total_weight": 0, "due": 0, "assigned": 0}

    # Weight = capacity_hint if an admin set one, else this machine's own
    # assigned+enabled account count. A machine with weight 0 (no accounts
    # assigned, no capacity_hint override) contributes nothing and gets
    # nothing - it simply sits idle until an admin assigns it accounts.
    weights: dict[int, int] = {}
    for m in machines:
        weights[m["id"]] = m["capacity_hint"] if m["capacity_hint"] is not None else m["account_count"]
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return {"machines": len(machines), "total_weight": 0, "due": 0, "assigned": 0}

    # Carried-forward room: subtract each machine's still-open prior-day
    # assignments from its cap before handing out anything fresh, so a
    # machine that's behind on yesterday's batch doesn't also get piled
    # with a full new share today.
    carried = conn.execute(
        """
        SELECT machine_id, count(*) AS n FROM fleet_assignments
        WHERE status IN ('assigned', 'claimed') AND work_date < %(today)s
        GROUP BY machine_id
        """,
        {"today": work_date},
    ).fetchall()
    carried_by_machine = {r["machine_id"]: r["n"] for r in carried}

    remaining_room: dict[int, int] = {}
    for m in machines:
        cap = weights[m["id"]] * per_account_daily_cap
        remaining_room[m["id"]] = max(0, cap - carried_by_machine.get(m["id"], 0))
    total_room = sum(remaining_room.values())

    due = list_due_trademarks(conn)
    # Exclude anything already assigned (this run's carried-forward rows,
    # or a row another concurrent planning call just claimed) - the
    # partial unique index would also catch this at INSERT time, but
    # filtering here keeps the share math (step below) accurate against
    # what's actually still assignable.
    already_assigned_ids = {
        r["my_trademark_id"] for r in conn.execute(
            "SELECT my_trademark_id FROM fleet_assignments WHERE status IN ('assigned', 'claimed')"
        ).fetchall()
    }
    due = [t for t in due if t["id"] not in already_assigned_ids]

    to_assign_count = min(len(due), total_room)
    if to_assign_count <= 0:
        return {"machines": len(machines), "total_weight": total_weight, "due": len(due), "assigned": 0}

    # Largest-remainder apportionment of to_assign_count across machines,
    # weighted by remaining_room, capped so no machine gets more than its
    # own remaining_room even from rounding.
    shares: dict[int, int] = {}
    remainders: list[tuple[float, int]] = []
    for mid, room in remaining_room.items():
        if total_room == 0:
            exact = 0.0
        else:
            exact = to_assign_count * (room / total_room)
        base = min(room, int(exact))
        shares[mid] = base
        remainders.append((exact - base, mid))
    leftover = to_assign_count - sum(shares.values())
    for _, mid in sorted(remainders, reverse=True):
        if leftover <= 0:
            break
        if shares[mid] < remaining_room[mid]:
            shares[mid] += 1
            leftover -= 1

    # Walk the priority-ordered due list, handing out each machine's share
    # in order - highest-priority/oldest-backlog rows land on whichever
    # machine has room first, in machine-id order (stable, not meaningful
    # beyond determinism).
    rows_to_insert: list[tuple[int, int]] = []  # (machine_id, my_trademark_id)
    idx = 0
    for mid in sorted(shares.keys()):
        take = shares[mid]
        for tm in due[idx: idx + take]:
            rows_to_insert.append((mid, tm["id"]))
        idx += take

    if not rows_to_insert:
        return {"machines": len(machines), "total_weight": total_weight, "due": len(due), "assigned": 0}

    # A single multi-row INSERT (not executemany, which psycopg only
    # exposes on a Cursor, not the plain Connection.execute this codebase
    # otherwise uses everywhere) — cheap enough at this scale (at most a
    # few thousand rows per planning run) and keeps the same conn.execute
    # idiom as every other write in this file.
    values_sql = ", ".join(f"(%(work_date)s, %(m{i})s, %(t{i})s)" for i in range(len(rows_to_insert)))
    params: dict = {"work_date": work_date}
    for i, (mid, tid) in enumerate(rows_to_insert):
        params[f"m{i}"] = mid
        params[f"t{i}"] = tid
    conn.execute(
        f"""
        INSERT INTO fleet_assignments (work_date, machine_id, my_trademark_id)
        VALUES {values_sql}
        ON CONFLICT DO NOTHING
        """,
        params,
    )
    conn.commit()
    return {
        "machines": len(machines), "total_weight": total_weight,
        "due": len(due), "assigned": len(rows_to_insert),
    }


def claim_fleet_assignment_rows_for_worker(conn: psycopg.Connection, machine_id: int, limit: int) -> list[dict]:
    """Coordinator-side half of a worker's GET /api/fleet/assignment poll
    (called from fleet_api.py) — flips this machine's own 'assigned' rows
    for TODAY to 'claimed' and returns them with their app_no, so a
    re-poll never hands out the same batch twice while a worker is still
    mid-batch. This is assignment-TRACKING (for the dashboard's
    pending/done/failed counts), not the fetch-safety claim itself — the
    worker's own claim_fleet_assigned_batch (run against its local
    my_trademarks/fleet_assignments over the same shared Postgres) does
    the real atomic FOR UPDATE SKIP LOCKED claim before anything is
    actually fetched. Losing/duplicating a row here would only ever show
    stale dashboard counts, never a double-fetch."""
    cursor = conn.execute(
        """
        UPDATE fleet_assignments
        SET status = 'claimed', claimed_at = now()
        WHERE id IN (
            SELECT fa.id FROM fleet_assignments fa
            WHERE fa.machine_id = %(machine_id)s AND fa.work_date = current_date AND fa.status = 'assigned'
            ORDER BY fa.id
            LIMIT %(limit)s
        )
        RETURNING id AS assignment_id, my_trademark_id
        """,
        {"machine_id": machine_id, "limit": limit},
    )
    rows = cursor.fetchall()
    conn.commit()
    if not rows:
        return []
    tm_ids = [r["my_trademark_id"] for r in rows]
    app_nos = {
        r["id"]: r["app_no"]
        for r in conn.execute("SELECT id, app_no FROM my_trademarks WHERE id = ANY(%(ids)s::int[])", {"ids": tm_ids}).fetchall()
    }
    for r in rows:
        r["app_no"] = app_nos.get(r["my_trademark_id"])
    return rows


def report_fleet_assignment_result(conn: psycopg.Connection, assignment_id: int, status: str) -> bool:
    if status not in ("done", "failed"):
        raise ValueError(f"Invalid fleet assignment result status: {status!r}")
    cursor = conn.execute(
        "UPDATE fleet_assignments SET status = %(status)s, completed_at = now() WHERE id = %(id)s",
        {"status": status, "id": assignment_id},
    )
    conn.commit()
    return cursor.rowcount > 0


def claim_fleet_assigned_batch(conn: psycopg.Connection, machine_id: int, batch_size: int) -> list[dict]:
    """Worker-side sibling of claim_batch_pending_trademarks (line ~1796),
    scoped to exactly this machine's own fleet_assignments slice instead of
    the global pending queue. Runs on the WORKER's own local scheduler
    (scheduler.py's _worker_loop, gated on config.FLEET_MODE_ENABLED),
    against the same shared Postgres the coordinator plans against. Same
    atomic UPDATE...RETURNING + FOR UPDATE SKIP LOCKED idiom as the global
    function, so concurrent callers (this machine's own _WORKERS threads)
    still can't double-claim a row. The existing global
    claim_batch_pending_trademarks is completely untouched by this
    addition — every non-fleet install keeps calling it exactly as
    before."""
    cursor = conn.execute(
        """
        UPDATE my_trademarks
        SET status = 'fetching', attempt_count = attempt_count + 1,
            last_attempted_at = now(), updated_at = now()
        WHERE id IN (
            SELECT t.id FROM my_trademarks t
            JOIN fleet_assignments fa ON fa.my_trademark_id = t.id
            WHERE fa.machine_id = %(machine_id)s
              AND fa.work_date = current_date
              AND fa.status IN ('assigned', 'claimed')
              AND (t.status = 'pending' OR (t.status = 'error' AND t.attempt_count < %(max_attempts)s))
            ORDER BY t.priority DESC, t.created_at DESC, t.attempt_count
            LIMIT %(batch_size)s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        {"machine_id": machine_id, "max_attempts": _MAX_AUTO_RETRY_ATTEMPTS, "batch_size": batch_size},
    )
    rows = cursor.fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        conn.execute(
            "UPDATE fleet_assignments SET status = 'claimed', claimed_at = COALESCE(claimed_at, now()) "
            "WHERE my_trademark_id = ANY(%(ids)s::int[]) AND status = 'assigned'",
            {"ids": ids},
        )
    conn.commit()
    return rows


_UI_ADD_PRIORITY = 100   # a trademark added through the UI jumps the fetch queue


def add_trademark(
    conn: psycopg.Connection,
    app_no: str,
    user_id: int,
    label: str | None = None,
    priority: int = _UI_ADD_PRIORITY,
    kind: str = "portfolio",
) -> tuple[int | None, str | None]:
    """Add one application number as 'pending', owned by *user_id*. Returns
    (id, None) on success, or (None, "already tracked") if this user is
    already tracking this app_no under this *kind*. `priority` defaults to
    _UI_ADD_PRIORITY so a UI add is fetched before the older backlog; pass
    priority=0 for bulk/migration adds.

    Unlike the read/list functions in this module, user_id here is always a
    real id, never None — every add happens on behalf of a specific logged-
    in user (an admin adding "for everyone" isn't a concept this app has).

    Two different users can track the same app_no independently (the
    uniqueness is on (user_id, app_no, kind), not app_no alone) — the
    underlying scraped data is shared/global (see get_trademark_data), only
    the tracking row itself is per-user. kind='portfolio' (default) is a
    mark this user owns; kind='opposition_out' is someone else's
    application this user's team is opposing — same fetch pipeline either
    way (see MY_TRADEMARKS_SCHEMA), just a different bucket, and one user
    can track the very same app_no under both kinds at once.

    If this user already has the app_no tracked (same kind) and it's still
    'pending', its priority is bumped to at least `priority` so
    re-submitting a mark also moves it up.
    """
    cursor = conn.execute(
        """
        INSERT INTO my_trademarks (app_no, label, priority, user_id, kind)
        VALUES (%(app_no)s, %(label)s, %(priority)s, %(user_id)s, %(kind)s)
        ON CONFLICT (user_id, app_no, kind) DO UPDATE
            SET priority = GREATEST(my_trademarks.priority, EXCLUDED.priority),
                updated_at = now()
            WHERE my_trademarks.status IN ('pending', 'error')
        RETURNING id, (xmax = 0) AS inserted
        """,
        {"app_no": app_no, "label": label, "priority": priority, "user_id": user_id, "kind": kind},
    )
    row = cursor.fetchone()
    if config.FLEET_CONTROL_ENABLED and row and row['inserted']:
        queue_fetch_request(conn, app_no, user_id, 'initial', priority)
    conn.commit()
    if row is None:
        # conflict on a row that's already 'fetched'/'fetching' — left as-is
        return None, "already tracked"
    if not row["inserted"]:
        return row["id"], "already tracked"   # existed, priority bumped
    return row["id"], None


def add_trademarks_bulk(
    conn: psycopg.Connection, app_nos: list[str], user_id: int, priority: int = _UI_ADD_PRIORITY,
    kind: str = "portfolio",
) -> dict:
    """Add many application numbers at once, all owned by *user_id* under
    *kind* (see add_trademark). Returns {"added": [...], "skipped_existing":
    [...], "added_ids": {app_no: id}}. added_ids lets the caller kick off an
    immediate fast fetch for exactly the rows that were newly added, without
    a second DB round-trip."""
    added: list[str] = []
    skipped: list[str] = []
    added_ids: dict[str, int] = {}
    for app_no in app_nos:
        id_, err = add_trademark(conn, app_no, user_id, priority=priority, kind=kind)
        if err:
            skipped.append(app_no)
        else:
            added.append(app_no)
            added_ids[app_no] = id_
    return {"added": added, "skipped_existing": skipped, "added_ids": added_ids}


def queue_fetch_request(conn, app_no, user_id, reason='manual', priority=150):
    """Caller commits this handoff with its user operation."""
    conn.execute('INSERT INTO public.fleet_fetch_requests(id,app_no,user_id,reason,priority) VALUES(%s,%s,%s,%s,%s)',
                 (str(uuid.uuid4()), app_no, user_id, reason, priority))


def request_fleet_refresh(conn, id_, user_id):
    tm = conn.execute('UPDATE my_trademarks SET status=%s,last_error=NULL,updated_at=now() '
                      'WHERE id=%s AND (%s::int IS NULL OR user_id=%s) RETURNING app_no,user_id',
                      ('pending', id_, user_id, user_id)).fetchone()
    if not tm:
        return False
    queue_fetch_request(conn, tm['app_no'], tm['user_id'])
    conn.commit()
    return True


def fleet_artifacts(conn, app_no):
    rows = conn.execute("SELECT r.worker_id,r.id,r.payload FROM fleet_control.results r "
                        "WHERE r.app_no=%s AND (r.disposition='accepted' OR (r.disposition='partial' AND r.payload->'stages'->'core'->>'state'='complete')) ORDER BY r.observed_at DESC", (app_no,)).fetchall()
    return [{**a, 'result_id': row['id'], 'worker_id': row['worker_id']} for row in rows for a in row['payload']['artifacts']]


def bump_trademark_priority(
    conn: psycopg.Connection, id_: int, user_id: int | None, priority: int = _UI_ADD_PRIORITY + 50,
) -> bool:
    """Push one mark to the very front of the fetch queue and re-queue it if
    it had errored. Wire this to a 'Fetch now' button.

    user_id: None (admin) bypasses ownership, a real id only updates a row
    that belongs to that user. Returns True if a row was actually updated —
    False means either the id doesn't exist or it belongs to someone else,
    which the caller should treat as a 404, not a 403 (see module-level
    ownership-check convention)."""
    cursor = conn.execute(
        """
        UPDATE my_trademarks
        SET priority = %(p)s,
            status = CASE WHEN status = 'error' THEN 'pending' ELSE status END,
            attempt_count = CASE WHEN status = 'error' THEN 0 ELSE attempt_count END,
            updated_at = now()
        WHERE id = %(id)s
          AND (%(user_id)s::int IS NULL OR user_id = %(user_id)s)
        """,
        {"id": id_, "p": priority, "user_id": user_id},
    )
    conn.commit()
    return cursor.rowcount > 0


# Fields only obtainable by re-parsing a trademark's saved e-Main-page HTML
# (extract_app_details_fields) — expensive to do for an entire portfolio on
# every list load, so list_trademarks only pays this cost for a column a
# user has actually chosen to see (see its extra_columns parameter).
_EXPENSIVE_COLUMN_KEYS = {"office", "country", "certificate_detail"}


def list_trademarks(
    conn: psycopg.Connection, user_id: int | None, extra_columns: list[str] | None = None,
    kind: str = "portfolio",
) -> list[dict]:
    # Portfolio view: each tracked row plus the portal fields worth showing in
    # a list (word mark, class, real IP India status, proprietor, logo) -
    # pulled from the same ipindia_results row last_search_id already points
    # to, and tma_documents for the class/logo extras the raw portal payload
    # doesn't carry on its own. Read-only join against the scraper's own
    # tables, same as get_trademark_data's single-record version.
    #
    # user_id: None (admin) sees every tenant's portfolio; a real id sees
    # only that user's own rows.
    #
    # extra_columns: the CALLER's own saved column choices (see
    # get_user_column_prefs) — only used to decide whether the expensive
    # e-Main-page HTML re-parse (_EXPENSIVE_COLUMN_KEYS) is worth doing at
    # all for this request. A user who never added Office/Country/
    # Certificate Detail to their view never pays that cost.
    #
    # Unfiltered/unpaginated PER-USER by design - a single tenant's portfolio
    # is a few hundred rows at most, so filtering (word mark search, class,
    # status) and pagination both happen in Python on this result (see
    # app.py's index route) rather than adding a second SQL path, since
    # portal_status/word_mark/class_number only exist after the data_json -
    # dict unpacking below, not as real columns to filter on in SQL.
    extra_columns = extra_columns or []
    needs_app_details = bool(_EXPENSIVE_COLUMN_KEYS & set(extra_columns))

    cursor = conn.execute(
        """
        SELECT
            t.*,
            r.data_json,
            tma.class_number   AS tma_class_number,
            tma.class_description,
            tma.mark_category,
            tma.logo_path,
            tma.mark_trade_mark,
            tma.applicant_name,
            tma.application_date,
            tma.application_filed_as
        FROM my_trademarks t
        LEFT JOIN ipindia_results r ON r.id = t.last_search_id
        LEFT JOIN LATERAL (
            SELECT class_number, class_description, mark_category, logo_path,
                   mark_trade_mark, applicant_name, application_date, application_filed_as
            FROM tma_documents
            WHERE app_no = t.app_no
            ORDER BY fleet_canonical DESC, extracted_at DESC
            LIMIT 1
        ) tma ON true
        WHERE (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
          AND t.kind = %(kind)s
        ORDER BY COALESCE(t.fetched_at, t.updated_at, t.created_at) DESC
        """,
        {"user_id": user_id, "kind": kind},
    )
    rows = cursor.fetchall()
    for row in rows:
        raw = row.pop("data_json", None)
        portal = (raw[0] if raw else {}) or {}
        # The portal search payload (data_json) is the richest source, but it
        # only exists once a row has been fetched live. For rows that haven't
        # (or whose fetch is still queued), fall back to the tma_documents
        # extract we already have on file — same fields, just parsed from the
        # TM-A form PDF instead of the status page.
        tma_mark = row.pop("mark_trade_mark", None)
        tma_applicant = row.pop("applicant_name", None)
        tma_app_date = row.pop("application_date", None)
        row["portal_status"] = portal.get("Status")
        row["word_mark"] = portal.get("Trade Mark") or tma_mark
        row["class_number"] = row.pop("tma_class_number", None) or portal.get("Class")
        row["proprietor_name"] = portal.get("Proprietor Name") or tma_applicant
        row["application_date"] = portal.get("Date of Application") or tma_app_date
        row["has_logo"] = bool(row.get("logo_path"))
        # Tier 1 — already-joined data_json keys, always free to surface.
        row["sub_status"] = portal.get("Sub Status")
        row["tm_type"] = portal.get("TM Type")
        row["state"] = portal.get("State")
        row["valid_upto"] = portal.get("Valid Upto/Renewed Upto")
        row["publication_date"] = portal.get("Publication Date") or portal.get("Publication date")
        # Filing Mode — cheap, from the same tma_documents row already joined.
        row["application_filed_as"] = row.get("application_filed_as")
        # Category is mark_category (already selected) under the name the
        # column picker uses — kept as its own key so the frontend doesn't
        # need to know the backend's internal tma column name.
        row["category"] = row.get("mark_category")

        if needs_app_details:
            app_details_row = get_ipindia_page_fetch(conn, row["app_no"], "app_details")
            app_details = extract_app_details_fields(app_details_row["raw_html"]) if app_details_row else {}
            row["office"] = app_details.get("office")
            row["country"] = app_details.get("country")
            row["certificate_detail"] = app_details.get("certificate_detail")

    # Portfolio list order is "most recently updated first" (SQL ORDER BY
    # above, on fetched_at/updated_at/created_at) — NOT sorted by the
    # trademark's own application_date. A previous version of this function
    # sorted by application_date here in Python, which silently overrode
    # the SQL order entirely: a mark re-fetched moments ago but originally
    # filed years back would sort to the very bottom instead of the top,
    # which is the opposite of what "show latest updated first" means.
    return rows


def get_trademark(conn: psycopg.Connection, id_: int, user_id: int | None) -> dict | None:
    """user_id: None (admin) can fetch any row; a real id only returns a row
    that user owns — a mismatch returns None, same as a nonexistent id, so a
    caller probing another tenant's numeric id can't tell the difference
    (routes should turn None into a 404, never a 403)."""
    cursor = conn.execute(
        "SELECT * FROM my_trademarks WHERE id = %(id)s AND (%(user_id)s::int IS NULL OR user_id = %(user_id)s)",
        {"id": id_, "user_id": user_id},
    )
    return cursor.fetchone()


# ── Google Drive migration: filename-keyed lookups for the serving routes ──
# A trademark can have MULTIPLE tma_documents rows over time (one per
# extraction, no upsert — see save_tma_document), so unlike the "just get
# the newest one" query used elsewhere, these three functions match on the
# EXACT filename the frontend/URL already carries, since that's the only
# way to find which specific row (and therefore which specific
# drive_file_id) a given saved file corresponds to.

def get_tma_document_by_filename(conn: psycopg.Connection, app_no: str, filename: str) -> dict | None:
    """Finds the tma_documents row whose source_pdf_filename matches
    *filename* exactly, newest first (in the rare case of a genuine
    duplicate filename across two extractions)."""
    cursor = conn.execute(
        """
        SELECT drive_file_id, source_pdf_path FROM tma_documents
        WHERE app_no = %(app_no)s AND source_pdf_filename = %(filename)s
        ORDER BY fleet_canonical DESC, extracted_at DESC LIMIT 1
        """,
        {"app_no": app_no, "filename": filename},
    )
    return cursor.fetchone()


def list_tma_documents(conn: psycopg.Connection, app_no: str) -> list[dict]:
    """All saved application documents for this app_no, newest first — the
    DB-backed replacement for the old "glob the local disk folder for
    *.pdf" listing logic. Reads source_pdf_filename directly rather than
    the local disk, so a document that uploaded straight to Drive (no
    local file at all, source_pdf_path NULL) still shows up here — the
    glob approach silently hid these once Drive-first uploads shipped."""
    cursor = conn.execute(
        """
        SELECT source_pdf_filename AS filename, extracted_at
        FROM tma_documents
        WHERE app_no = %(app_no)s AND source_pdf_filename IS NOT NULL AND NOT fleet_canonical
        ORDER BY fleet_canonical DESC, extracted_at DESC
        """,
        {"app_no": app_no},
    )
    documents = cursor.fetchall()
    if config.FLEET_CONTROL_ENABLED:
        seen = {d['filename'] for d in documents}
        for artifact in fleet_artifacts(conn, app_no):
            if artifact['kind'] == 'document' and artifact['filename'] not in seen:
                documents.append({'filename': artifact['filename']})
                seen.add(artifact['filename'])
    return documents


def get_tma_logo_drive_id(conn: psycopg.Connection, app_no: str, filename: str) -> str | None:
    """Same idea as get_tma_document_by_filename but for the separate logo
    PNG a tma_documents row can reference — matched by the logo's own
    filename (derived from the source PDF's stem, e.g.
    "<stem>-logo.png"), stored in logo_path when it lives locally."""
    cursor = conn.execute(
        """
        SELECT logo_drive_file_id FROM tma_documents
        WHERE app_no = %(app_no)s AND logo_path LIKE %(pattern)s
        ORDER BY fleet_canonical DESC, extracted_at DESC LIMIT 1
        """,
        {"app_no": app_no, "pattern": f"%{filename}"},
    )
    row = cursor.fetchone()
    return row["logo_drive_file_id"] if row else None


def get_reminder_drive_id(conn: psycopg.Connection, app_no: str, filename: str) -> str | None:
    """document_path is normalized to a bare filename by
    _strip_notice_folder_prefix wherever it's read for display — match the
    same way here (LIKE on the trailing segment) so a value stored with a
    historical 'reminders\\' prefix still matches the bare filename the
    frontend/URL sends."""
    cursor = conn.execute(
        """
        SELECT drive_file_id FROM ipindia_reminders
        WHERE app_no = %(app_no)s AND document_path LIKE %(pattern)s
        ORDER BY fetched_at DESC LIMIT 1
        """,
        {"app_no": app_no, "pattern": f"%{filename}"},
    )
    row = cursor.fetchone()
    return row["drive_file_id"] if row else None


def get_correspondence_drive_id(conn: psycopg.Connection, app_no: str, filename: str) -> str | None:
    """See get_reminder_drive_id — identical matching logic, other table."""
    cursor = conn.execute(
        """
        SELECT drive_file_id FROM ipindia_correspondence
        WHERE app_no = %(app_no)s AND document_path LIKE %(pattern)s
        ORDER BY fetched_at DESC LIMIT 1
        """,
        {"app_no": app_no, "pattern": f"%{filename}"},
    )
    row = cursor.fetchone()
    return row["drive_file_id"] if row else None


def get_glob_logo_drive_id(conn: psycopg.Connection, app_no: str, local_filename: str) -> str | None:
    """Looks up a Drive id for a logo image that has NO tma_documents row
    at all — e.g. trademark-image.png, saved by a separate code path
    (IPIndiaSession.get_trademark_image) outside process_pdf. Populated
    only by the standalone cleanup script's backfill pass (Phase 5) — the
    live upload hook for THIS specific file type doesn't exist yet (it's
    saved by the scraper directly, not through process_pdf), so today
    every row here comes from that backfill, not from a live upload."""
    cursor = conn.execute(
        "SELECT drive_file_id FROM drive_logo_files WHERE app_no = %(app_no)s AND local_filename = %(filename)s",
        {"app_no": app_no, "filename": local_filename},
    )
    row = cursor.fetchone()
    return row["drive_file_id"] if row else None


def update_trademark_notes(conn: psycopg.Connection, id_: int, user_id: int | None, notes: str) -> bool:
    """Returns True if a row was updated — False means not found or not
    owned by this user (caller should 404, see get_trademark)."""
    cursor = conn.execute(
        """
        UPDATE my_trademarks SET notes = %(notes)s, updated_at = now()
        WHERE id = %(id)s AND (%(user_id)s::int IS NULL OR user_id = %(user_id)s)
        """,
        {"id": id_, "notes": notes, "user_id": user_id},
    )
    conn.commit()
    return cursor.rowcount > 0


def find_cached_appno_search(conn: psycopg.Connection, app_no: str) -> int | None:
    """Most recent single-application ipindia_results row already on file
    for this application number, if any — lets TM Search show a cached
    result instantly instead of always hitting the live (and sometimes
    down) IP India portal, same read-only sharing as get_trademark_data
    below. Matches both 'appno' (manual/API search) and 'appno_discover'
    (the auto-discover series-scanner) — both are single-record results
    keyed by exactly this app_no; only 'name' (word-mark search, which can
    return many unrelated app numbers in one row) is excluded."""
    cursor = conn.execute(
        """
        SELECT id FROM ipindia_results
        WHERE query = %(app_no)s AND search_type IN ('appno', 'appno_discover', 'appno_batch') AND error = ''
        ORDER BY scraped_at DESC LIMIT 1
        """,
        {"app_no": app_no},
    )
    row = cursor.fetchone()
    return row["id"] if row else None


def record_search_history(conn: psycopg.Connection, user_id: int, app_no: str, search_id: int | None) -> None:
    """Log one TM Search lookup for *user_id* — called right when a search
    completes (see app.py's POST /api/search-history). Kept in TM Manager's
    own table rather than the scraper's shared ipindia_results, so per-user
    search history doesn't require touching the scraper's schema/code."""
    conn.execute(
        "INSERT INTO tm_search_history (user_id, app_no, search_id) VALUES (%(user_id)s, %(app_no)s, %(search_id)s)",
        {"user_id": user_id, "app_no": app_no, "search_id": search_id},
    )
    conn.commit()


def count_searches_since(conn: psycopg.Connection, user_id: int, since: datetime) -> int:
    """How many TM Searches *user_id* has logged since *since* — used by
    billing.check_can_search to enforce a plan's per-period search_limit.
    Counts tm_search_history rows only (manual TM Search lookups), NOT
    Auto Discover/Live Refresh/Word Mark — see billing.py's module
    docstring for why only this one action is metered."""
    row = conn.execute(
        "SELECT count(*) AS n FROM tm_search_history WHERE user_id = %(user_id)s AND searched_at >= %(since)s",
        {"user_id": user_id, "since": since},
    ).fetchone()
    return row["n"] if row else 0


def list_recent_appno_searches(conn: psycopg.Connection, user_id: int | None, limit: int = 30) -> list[dict]:
    """Recent TM Search history for the TM Search page, scoped to *user_id*
    (None = admin sees everyone's) via TM Manager's own tm_search_history
    log — joined back to the scraper's shared ipindia_results for the
    trade mark/status fields to render a history row without a second
    round-trip per item. A lookup that hasn't landed in ipindia_results
    yet (search_id NULL, or the row since deleted) still shows up with
    just its app_no — better than silently dropping recent history."""
    cursor = conn.execute(
        """
        SELECT h.search_id, h.app_no, h.searched_at,
               r.error, r.scraped_at,
               r.data_json->0->>'Trade Mark' AS trade_mark,
               r.data_json->0->>'Status' AS status
        FROM tm_search_history h
        LEFT JOIN ipindia_results r ON r.id = h.search_id
        WHERE (%(user_id)s::int IS NULL OR h.user_id = %(user_id)s)
        ORDER BY h.searched_at DESC
        LIMIT %(limit)s
        """,
        {"limit": limit, "user_id": user_id},
    )
    return cursor.fetchall()


def get_trademark_data(conn: psycopg.Connection, app_no: str, search_id: int) -> dict | None:
    # Read-only lookup of EVERYTHING known about one fetched application -
    # the full raw portal-status fields (data_json - whatever labels the
    # portal returned, shown as-is, not a curated subset), every extracted
    # TM-A form field, every correspondence/reminder notice on file, and the
    # list of saved documents. Pulls from the shared ipindia_results
    # (search_id, the exact scrape this trademark's tracking row points to),
    # tma_documents (app_no, latest by extracted_at), ipindia_correspondence,
    # and ipindia_reminders tables - all populated by the fetching process.
    # This app never writes to any of them; it only reads, so client-facing
    # pages can show the complete picture without any reference to how or
    # where it was fetched from.
    cursor = conn.execute(
        "SELECT data_json FROM ipindia_results WHERE id = %(id)s",
        {"id": search_id},
    )
    result_row = cursor.fetchone()
    data = (result_row["data_json"][0] if result_row and result_row.get("data_json") else {}) or {}
    # Every label the portal returned, in the order it returned them -
    # shown as-is so nothing the portal reports is ever hidden.
    portal_fields = [(k, v) for k, v in data.items() if k not in ("query",) and v not in (None, "")]

    cursor = conn.execute(
        "SELECT * FROM tma_documents WHERE app_no = %(app_no)s ORDER BY fleet_canonical DESC, extracted_at DESC LIMIT 1",
        {"app_no": app_no},
    )
    tma = cursor.fetchone() or {}

    def extracted_value(column, field, *fallbacks):
        source = tma.get('raw_json') or {}
        for part in field.split('.'):
            if not isinstance(source, dict) or part not in source:
                source = 'not_matched'
                break
            source = source[part]
        if tma.get('fleet_canonical') and source is None:
            return None  # A newer explicit blank must not resurrect old data.
        return tma.get(column) or next((value for value in fallbacks if value), None)

    cursor = conn.execute(
        "SELECT * FROM ipindia_correspondence WHERE app_no = %(app_no)s ORDER BY fetched_at ASC, id ASC",
        {"app_no": app_no},
    )
    correspondence = cursor.fetchall()

    cursor = conn.execute(
        "SELECT * FROM ipindia_reminders WHERE app_no = %(app_no)s ORDER BY fetched_at ASC, id ASC",
        {"app_no": app_no},
    )
    reminders = cursor.fetchall()

    # Independent of tma_documents.logo_path on purpose — the portal's own
    # directly-fetched trademark image is available even for hits with no
    # uploaded documents at all, or where the TM-A PDF never parsed. See
    # ipindia_trademark_images's schema comment (root db.py) for why this
    # couldn't live on tma_documents itself.
    direct_image_row = conn.execute(
        "SELECT image_path FROM ipindia_trademark_images WHERE app_no = %(app_no)s",
        {"app_no": app_no},
    ).fetchone()
    has_direct_image = bool(direct_image_row and direct_image_row.get("image_path"))

    # The "e-main page" (RegisteredTM/Details — see root ipindia_scraper.py's
    # get_app_details) often carries fields the main status-page scrape
    # (data_json above) never returns at all: State, Office, Country,
    # Certificate Detail, and full Attorney/Proprietor address/email. Parsed
    # here (extract_app_details_fields) rather than at fetch time, so an
    # improved parser applies to already-fetched raw HTML without a
    # backfill. Used only as a FALLBACK below — data_json (the live status
    # scrape) wins where both have a value, since it's the more current
    # source; the e-main page fills in the gaps.
    app_details_row = get_ipindia_page_fetch(conn, app_no, "app_details")
    app_details = extract_app_details_fields(app_details_row["raw_html"]) if app_details_row else {}

    pr_details_row = get_ipindia_page_fetch(conn, app_no, "pr_details")
    pr_details_text = extract_pr_details_text(pr_details_row["raw_html"]) if pr_details_row else ""

    return {
        "portal_fields": portal_fields,
        "trade_mark": extracted_value('mark_trade_mark', 'mark.trade_mark', data.get('Trade Mark'), app_details.get('trade_mark')),
        "mark_category": tma.get("mark_category"),
        "mark_image_description": tma.get("mark_image_description"),
        "status": data.get("Status"),
        "sub_status": data.get("Sub Status"),
        "tm_type": data.get("TM Type") or app_details.get("tm_type"),
        "class_number": extracted_value('class_number', 'class.number', data.get('Class'), app_details.get('class_number')),
        "class_description": tma.get("class_description"),
        "application_date": tma.get("application_date") or data.get("Date of Application") or app_details.get("application_date"),
        "valid_upto": data.get("Valid Upto/Renewed Upto") or app_details.get("valid_upto"),
        "use_statement": extracted_value('use_statement', 'use_statement', data.get('User Detail'), app_details.get('user_detail')),
        "application_filed_as": tma.get("application_filed_as") or app_details.get("filing_mode"),
        "state": data.get("State") or app_details.get("state"),
        "office": app_details.get("office"),
        "country": app_details.get("country"),
        "category": app_details.get("category"),
        "certificate_detail": app_details.get("certificate_detail"),
        "publication_date": data.get("Publication Date") or data.get("Publication date"),
        "language": tma.get("language"),
        "temp_ref_no": tma.get("temp_ref_no"),
        "fee": tma.get("fee"),
        "receipt_no": tma.get("receipt_no"),
        "receipt_date": tma.get("receipt_date"),
        "receipt_amount": tma.get("receipt_amount"),
        "form_no": tma.get("form_no"),
        "registry_office": tma.get("registry_office") or app_details.get("office"),
        "verification_date": tma.get("verification_date"),
        "verification_signed_for": tma.get("verification_signed_for"),
        "other_information": tma.get("other_information"),
        "applicant_name": extracted_value('applicant_name', 'applicant.name', data.get('Proprietor Name'), app_details.get('proprietor_name')),
        "applicant_partners": tma.get("applicant_partners"),
        "applicant_address": extracted_value('applicant_address', 'applicant.address', app_details.get('proprietor_address')),
        "applicant_address_for_service": tma.get("applicant_address_for_service"),
        "applicant_country": tma.get("applicant_country"),
        "applicant_jurisdiction": tma.get("applicant_jurisdiction"),
        "applicant_nature": tma.get("applicant_nature"),
        "applicant_legal_status": tma.get("applicant_legal_status"),
        "applicant_mobile": tma.get("applicant_mobile"),
        "applicant_email": extracted_value('applicant_email', 'applicant.email', app_details.get('email')),
        "agent_name": extracted_value('agent_name', 'agent.name', app_details.get('attorney_name')),
        "agent_address": extracted_value('agent_address', 'agent.address', app_details.get('attorney_address')),
        "agent_nature": tma.get("agent_nature"),
        "agent_registration_no": tma.get("agent_registration_no"),
        "agent_code": tma.get("agent_code"),
        "proprietor_code": tma.get("proprietor_code"),
        "goods_and_services": app_details.get("goods_and_services"),
        "pr_details_text": pr_details_text,
        "has_logo": has_direct_image or bool(tma.get("logo_path")),
        "has_document": bool(tma.get("source_pdf_filename")),
        "correspondence": correspondence,
        "reminders": reminders,
    }


# Portal label -> our field name, for the e-main page's plain label/value
# table (RegisteredTM/Details — see root ipindia_scraper.py's
# IPIndiaSession.get_app_details). Duplicated from root ipindia_scraper.py's
# identical _APP_DETAILS_FIELD_MAP/extract_app_details_fields on purpose —
# this app "never imports [the scraper's] code directly" (see this file's
# own module docstring / app.py's), and a plain regex parser here avoids
# adding a BeautifulSoup dependency for one small extraction. Keep both
# copies in sync if the portal's table layout ever changes.
_APP_DETAILS_FIELD_MAP = {
    "tm application no.": "app_no",
    "class": "class_number",
    "date of application": "application_date",
    "appropriate office": "office",
    "state": "state",
    "country": "country",
    "filing mode": "filing_mode",
    "tm applied for": "trade_mark",
    "tm category": "category",
    "trade mark type": "tm_type",
    "user detail": "user_detail",
    "certificate detail": "certificate_detail",
    "valid upto/ renewed upto": "valid_upto",
    "proprietor name": "proprietor_name",
    "proprietor address": "proprietor_address",
    "email id": "email",
    "attorney name": "attorney_name",
    "attorney address": "attorney_address",
    "goods & service details": "goods_and_services",
}

_APP_DETAILS_ROW_RE = re.compile(
    r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_app_details_fields(raw_html: str) -> dict:
    """
    Parse the e-main page's raw HTML into a flat dict of structured fields —
    State, Office, Country, Certificate Detail, etc. See root
    ipindia_scraper.py's identical function for the full rationale; this is
    that same parser, re-implemented with plain regex instead of
    BeautifulSoup (not a dependency here) since this app never imports the
    scraper's code directly. Returns {} on anything that doesn't parse.
    """
    if not raw_html:
        return {}
    fields: dict = {}
    for label_html, value_html in _APP_DETAILS_ROW_RE.findall(raw_html):
        # unescape BEFORE matching the field map — the portal HTML-escapes
        # "Goods & Service Details" as "Goods &amp; Service Details", which
        # silently missed the map (a literal "&" key) without this.
        label = re.sub(r"\s+", " ", _TAG_RE.sub("", _html.unescape(label_html))).strip().lower()
        field_name = _APP_DETAILS_FIELD_MAP.get(label)
        if not field_name:
            continue
        # <br> -> newline before stripping the rest of the tags, so a
        # multi-line cell (proprietor name + "Single Firm", goods list)
        # doesn't get its line breaks squashed into one run-on string.
        value = _TAG_RE.sub(
            lambda m: "\n" if m.group(0).lower().startswith("<br") else "",
            _html.unescape(value_html),
        )
        value = re.sub(r"[ \t]+", " ", value).strip()
        value = "\n".join(line.strip() for line in value.split("\n") if line.strip())
        if value:
            fields[field_name] = value
    return fields


def extract_pr_details_text(raw_html: str) -> str:
    """
    PR Details (proprietor/registration change history) is plain prose or
    "No Record Found" — not a label/value table like the e-main page, so
    there's nothing to map field-by-field. This just strips tags/collapses
    whitespace so the frontend shows readable text instead of a raw HTML
    dump; the "no history" case is filtered out in the frontend rather than
    here, so a caller that wants to know the difference between "genuinely
    no history" and "not fetched at all" still can (empty string here means
    not fetched; a non-empty "No Record Found" case is a real, if boring,
    answer).
    """
    if not raw_html:
        return ""
    text = _html.unescape(_TAG_RE.sub(" ", raw_html))
    return re.sub(r"[ \t]+", " ", text).strip()


def get_ipindia_page_fetch(conn: psycopg.Connection, app_no: str, kind: str) -> dict | None:
    """Raw HTML for the 'pr_details' or 'app_details' (e-main page) panel —
    written by the scraper's root db.py (_fetch_all_tm_panels in app.py),
    read here directly since ipindia_page_fetches lives in the shared
    Postgres database but isn't owned by this module's own schema (same
    cross-module pattern the direct-image lookup above already uses)."""
    cursor = conn.execute(
        "SELECT raw_html FROM ipindia_page_fetches WHERE app_no = %(app_no)s AND kind = %(kind)s",
        {"app_no": app_no, "kind": kind},
    )
    return cursor.fetchone()


def list_fetch_history(conn: psycopg.Connection, app_no: str) -> list[dict]:
    """
    Every past fetch of this application number, newest first - refetching
    never overwrites anything; each fetch adds a new row to the shared
    ipindia_results table, so the full history has been there all along.
    Returns just (id, scraped_at) pairs; call get_trademark_data(app_no, id)
    for any one snapshot's full details.
    """
    cursor = conn.execute(
        "SELECT id, scraped_at FROM ipindia_results WHERE query = %(app_no)s AND search_type IN ('appno', 'appno_discover', 'appno_batch') ORDER BY scraped_at DESC",
        {"app_no": app_no},
    )
    return cursor.fetchall()


def list_recent_status_changes(
    conn: psycopg.Connection, user_id: int | None, limit: int = 12, kind: str = "portfolio",
) -> list[dict]:
    """
    Portfolio marks whose IP India portal status changed between fetches, most
    recent change first. Reads the tm_status_history ledger (see
    record_status_snapshot) — every status change ever recorded is here, not
    just the single latest one, and the diff was computed at fetch time.
    user_id: None (admin) sees every tenant's changes; a real id sees only
    changes on that user's own portfolio. kind scopes to portfolio marks
    (default) or opposition-target marks (see MY_TRADEMARKS_SCHEMA) — kept
    separate so an outbound opposition's status change doesn't show up
    mixed into the main portfolio dashboard feed, and vice versa.
    """
    cursor = conn.execute(
        """
        SELECT h.my_trademark_id AS id, h.app_no, t.label,
               h.trade_mark      AS newest_trade_mark,
               h.proprietor_name AS newest_proprietor,
               h.changed_fields -> 'status' ->> 'from' AS prev_status,
               h.changed_fields -> 'status' ->> 'to'   AS newest_status,
               h.fetched_at AS newest_at,
               (tma.logo_path IS NOT NULL OR EXISTS (SELECT 1 FROM ipindia_trademark_images ti WHERE ti.app_no = t.app_no)) AS has_logo
        FROM tm_status_history h
        JOIN my_trademarks t ON t.id = h.my_trademark_id
        LEFT JOIN LATERAL (
            SELECT logo_path FROM tma_documents
            WHERE app_no = h.app_no ORDER BY fleet_canonical DESC, extracted_at DESC LIMIT 1
        ) tma ON true
        WHERE h.changed_fields ? 'status'
          AND h.is_first = false
          AND (h.changed_fields -> 'status' ->> 'from') IS NOT NULL
          AND (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
          AND t.kind = %(kind)s
        ORDER BY h.fetched_at DESC, h.id DESC
        LIMIT %(limit)s
        """,
        {"limit": limit, "user_id": user_id, "kind": kind},
    )
    changes = []
    for row in cursor.fetchall():
        changes.append({
            "id": row["id"],
            "app_no": row["app_no"],
            "word_mark": row["newest_trade_mark"] or row["label"] or row["app_no"],
            "proprietor_name": row["newest_proprietor"],
            "has_logo": bool(row["has_logo"]),
            "old_status": row["prev_status"],
            "new_status": row["newest_status"],
            "changed_at": row["newest_at"],
        })
    return changes


def list_status_changes_since(conn: psycopg.Connection, user_id: int, since) -> list[dict]:
    """Every status change on user_id's own trademarks recorded strictly
    after `since` — used by the daily digest email (scheduler._digest_loop)
    to send exactly what changed since the last digest, no more and no
    less. Deliberately not filtered by kind — a status change on an
    Outbound Opposition target belongs in the same daily digest as a
    portfolio mark's change, same "you'll be notified the same way" as
    portfolio marks (see MY_TRADEMARKS_SCHEMA's kind column). Same shape as
    list_recent_status_changes's per-row dict, but time-scoped instead of
    count-limited, and always for a real user (no admin/None bypass — the
    digest is per-account, never a combined one)."""
    cursor = conn.execute(
        """
        SELECT h.my_trademark_id AS id, h.app_no, t.label,
               h.trade_mark      AS newest_trade_mark,
               h.proprietor_name AS newest_proprietor,
               h.changed_fields -> 'status' ->> 'from' AS prev_status,
               h.changed_fields -> 'status' ->> 'to'   AS newest_status,
               h.fetched_at AS newest_at,
               (tma.logo_path IS NOT NULL OR EXISTS (SELECT 1 FROM ipindia_trademark_images ti WHERE ti.app_no = t.app_no)) AS has_logo
        FROM tm_status_history h
        JOIN my_trademarks t ON t.id = h.my_trademark_id
        LEFT JOIN LATERAL (
            SELECT logo_path FROM tma_documents
            WHERE app_no = h.app_no ORDER BY fleet_canonical DESC, extracted_at DESC LIMIT 1
        ) tma ON true
        WHERE h.changed_fields ? 'status'
          AND h.is_first = false
          AND (h.changed_fields -> 'status' ->> 'from') IS NOT NULL
          AND t.user_id = %(user_id)s
          AND h.fetched_at > %(since)s
        ORDER BY h.fetched_at ASC, h.id ASC
        """,
        {"user_id": user_id, "since": since},
    )
    return [
        {
            "id": row["id"],
            "app_no": row["app_no"],
            "word_mark": row["newest_trade_mark"] or row["label"] or row["app_no"],
            "proprietor_name": row["newest_proprietor"],
            "has_logo": bool(row["has_logo"]),
            "old_status": row["prev_status"],
            "new_status": row["newest_status"],
            "changed_at": row["newest_at"],
        }
        for row in cursor.fetchall()
    ]


def list_active_users_for_digest(conn: psycopg.Connection) -> list[dict]:
    """Every active account with at least one trademark — no point emailing
    someone with an empty portfolio. Returns id, email, display_name,
    last_digest_sent_at so the caller can compute each user's own `since`."""
    return conn.execute(
        """
        SELECT u.id, u.email, u.display_name, u.last_digest_sent_at
        FROM users u
        WHERE u.is_active
          AND EXISTS (SELECT 1 FROM my_trademarks t WHERE t.user_id = u.id)
        """
    ).fetchall()


def mark_digest_sent(conn: psycopg.Connection, user_id: int, sent_at) -> None:
    conn.execute("UPDATE users SET last_digest_sent_at = %s WHERE id = %s", (sent_at, user_id))
    conn.commit()


def monthly_filing_counts(
    conn: psycopg.Connection, user_id: int | None, months: int = 10, kind: str = "portfolio",
) -> list[dict]:
    """
    Count of tracked trademarks by application month, oldest-newest, for the
    filing-trend chart. Uses my_trademarks.created_at (when this app started
    tracking it) - application_date lives inside each fetch's data_json, not
    as a real column, and isn't set at all until the first fetch completes,
    so created_at is the only date guaranteed to exist for every row
    (fetched or not) and is what "when did this enter the portfolio" means
    for a portfolio-growth chart anyway. user_id: None (admin) counts every
    tenant's trademarks; a real id counts only that user's own. kind scopes
    to portfolio marks (default), excluding opposition-target rows from this
    portfolio-growth chart.
    """
    cursor = conn.execute(
        """
        SELECT to_char(date_trunc('month', created_at), 'YYYY-MM') AS ym, count(*) AS n
        FROM my_trademarks
        WHERE created_at >= date_trunc('month', now()) - make_interval(months => %(months)s - 1)
          AND (%(user_id)s::int IS NULL OR user_id = %(user_id)s)
          AND kind = %(kind)s
        GROUP BY ym
        ORDER BY ym
        """,
        {"months": months, "user_id": user_id, "kind": kind},
    )
    return cursor.fetchall()


def count_by_status(conn: psycopg.Connection, user_id: int | None, kind: str = "portfolio") -> dict:
    cursor = conn.execute(
        """
        SELECT status, count(*) AS n FROM my_trademarks
        WHERE (%(user_id)s::int IS NULL OR user_id = %(user_id)s)
          AND kind = %(kind)s
        GROUP BY status
        """,
        {"user_id": user_id, "kind": kind},
    )
    return {row["status"]: row["n"] for row in cursor.fetchall()}


def count_by_portal_status(conn: psycopg.Connection, user_id: int | None, kind: str = "portfolio") -> list[dict]:
    """Breakdown by the real IP India portal status (e.g. 'Registered',
    'Objected'), most common first - for the dashboard's status-wise panel.
    Rows with no fetched data yet are excluded (nothing to group them by).
    kind scopes to portfolio marks (default), excluding opposition-target
    rows from this portfolio-only panel."""
    cursor = conn.execute(
        """
        WITH statuses AS (
            SELECT (r.data_json -> 0 ->> 'Status') AS status
            FROM my_trademarks t
            JOIN ipindia_results r ON r.id = t.last_search_id
            WHERE (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
              AND t.kind = %(kind)s
        )
        SELECT status, count(*) AS n
        FROM statuses
        WHERE status IS NOT NULL
        GROUP BY status
        ORDER BY n DESC
        """,
        {"user_id": user_id, "kind": kind},
    )
    return cursor.fetchall()


def count_by_class(conn: psycopg.Connection, user_id: int | None, kind: str = "portfolio") -> list[dict]:
    """Breakdown by trademark class, most common first - for the
    dashboard's class-wise panel. Prefers tma_documents.class_number (more
    reliable) and falls back to the portal's own Class field. kind scopes to
    portfolio marks (default), excluding opposition-target rows."""
    cursor = conn.execute(
        """
        WITH classes AS (
            SELECT COALESCE(tma.class_number, r.data_json -> 0 ->> 'Class') AS class_number
            FROM my_trademarks t
            LEFT JOIN ipindia_results r ON r.id = t.last_search_id
            LEFT JOIN LATERAL (
                SELECT class_number
                FROM tma_documents
                WHERE app_no = t.app_no
                ORDER BY fleet_canonical DESC, extracted_at DESC
                LIMIT 1
            ) tma ON true
            WHERE (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
              AND t.kind = %(kind)s
        )
        SELECT class_number, count(*) AS n
        FROM classes
        WHERE class_number IS NOT NULL
        GROUP BY class_number
        ORDER BY n DESC
        """,
        {"user_id": user_id, "kind": kind},
    )
    return cursor.fetchall()


def count_by_state(conn: psycopg.Connection, user_id: int | None) -> list[dict]:
    """Breakdown by applicant state, most common first - for the dashboard's
    state-wise panel. State is read from the portal's own data_json ('State'
    field) since tma_documents does not carry a separate state column.
    Rows with no fetched data yet are excluded."""
    cursor = conn.execute(
        """
        SELECT (r.data_json -> 0 ->> 'State') AS state, count(*) AS n
        FROM my_trademarks t
        JOIN ipindia_results r ON r.id = t.last_search_id
        WHERE (r.data_json -> 0 ->> 'State') IS NOT NULL
          AND (r.data_json -> 0 ->> 'State') != ''
          AND (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
        GROUP BY state
        ORDER BY n DESC
        """,
        {"user_id": user_id},
    )
    return cursor.fetchall()


def count_opposed(conn: psycopg.Connection, user_id: int | None) -> int:
    """Count of trademarks whose real IP India portal status mentions
    'Opposed' - the only real opposition signal this app tracks. There is
    no separate inbound/outbound opposition record anywhere in the schema,
    so this is a single combined count, not a split."""
    cursor = conn.execute(
        """
        SELECT count(*) AS n
        FROM my_trademarks t
        JOIN ipindia_results r ON r.id = t.last_search_id
        WHERE (r.data_json -> 0 ->> 'Status') ILIKE '%%opposed%%'
          AND (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
        """,
        {"user_id": user_id},
    )
    return cursor.fetchone()["n"]


def upcoming_renewals(conn: psycopg.Connection, user_id: int | None, days_ahead: int = 30) -> list[dict]:
    """Trademarks whose 'Valid Upto/Renewed Upto' portal date falls within
    the next `days_ahead` days - the one genuine forward-looking due-date
    this app has (reminders/correspondence are all retrospective, see
    list_reminders). The date is parsed in Python since it lives inside
    data_json as a free-text DD/MM/YYYY string, not a real date column."""
    cursor = conn.execute(
        """
        SELECT t.id, t.app_no, t.label, r.data_json -> 0 ->> 'Trade Mark' AS word_mark,
               r.data_json -> 0 ->> 'Valid Upto/Renewed Upto' AS valid_upto,
               (tma.logo_path IS NOT NULL OR EXISTS (SELECT 1 FROM ipindia_trademark_images ti WHERE ti.app_no = t.app_no)) AS has_logo
        FROM my_trademarks t
        JOIN ipindia_results r ON r.id = t.last_search_id
        LEFT JOIN LATERAL (
            SELECT logo_path FROM tma_documents
            WHERE app_no = t.app_no AND logo_path IS NOT NULL
            ORDER BY fleet_canonical DESC, extracted_at DESC LIMIT 1
        ) tma ON true
        WHERE (r.data_json -> 0 ->> 'Valid Upto/Renewed Upto') IS NOT NULL
          AND (r.data_json -> 0 ->> 'Valid Upto/Renewed Upto') != ''
          AND (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
        """,
        {"user_id": user_id},
    )
    rows = cursor.fetchall()

    today = datetime.now().date()
    horizon = today + timedelta(days=days_ahead)
    result = []
    for row in rows:
        due = _parse_ddmmyyyy(row["valid_upto"])
        if due is None or not (today <= due <= horizon):
            continue
        row["due_date"] = due.isoformat()
        row["days_until"] = (due - today).days
        result.append(row)
    result.sort(key=lambda r: r["due_date"])
    return result


def _parse_ddmmyyyy(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip().split(" ")[0], "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None


def list_custom_tasks(conn: psycopg.Connection, user_id: int | None) -> list[dict]:
    """Every user-created task on the Tasks & Compliances page, oldest due
    date first. LEFT JOIN since my_trademark_id is nullable (a task doesn't
    have to be linked to a specific trademark)."""
    cursor = conn.execute(
        """
        SELECT ct.id, ct.title, ct.due_date, ct.status, ct.my_trademark_id,
               ct.created_at, ct.updated_at,
               t.app_no, t.label,
               (res.data_json -> 0 ->> 'Trade Mark') AS word_mark,
               (tma.logo_path IS NOT NULL OR EXISTS (SELECT 1 FROM ipindia_trademark_images ti WHERE ti.app_no = t.app_no)) AS has_logo
        FROM custom_tasks ct
        LEFT JOIN my_trademarks t ON t.id = ct.my_trademark_id
        LEFT JOIN ipindia_results res ON res.id = t.last_search_id
        LEFT JOIN LATERAL (
            SELECT logo_path FROM tma_documents
            WHERE app_no = t.app_no ORDER BY fleet_canonical DESC, extracted_at DESC LIMIT 1
        ) tma ON true
        WHERE (%(user_id)s::int IS NULL OR ct.user_id = %(user_id)s)
        ORDER BY ct.due_date ASC, ct.id ASC
        """,
        {"user_id": user_id},
    )
    return cursor.fetchall()


def create_custom_task(
    conn: psycopg.Connection, user_id: int, title: str, due_date: str, my_trademark_id: int | None
) -> dict | None:
    """Returns None (caller should treat as 400/404) if my_trademark_id is
    set but doesn't belong to user_id — without this check, a task could
    link to another tenant's my_trademarks row, and list_custom_tasks'
    LEFT JOIN would then surface that row's app_no/word_mark/has_logo to
    the wrong tenant (it only scopes by custom_tasks.user_id, not by the
    joined trademark's owner)."""
    if my_trademark_id is not None and get_trademark(conn, my_trademark_id, user_id) is None:
        return None
    cursor = conn.execute(
        """
        INSERT INTO custom_tasks (user_id, title, due_date, my_trademark_id)
        VALUES (%(user_id)s, %(title)s, %(due_date)s, %(my_trademark_id)s)
        RETURNING id, title, due_date, status, my_trademark_id, created_at, updated_at
        """,
        {"user_id": user_id, "title": title, "due_date": due_date, "my_trademark_id": my_trademark_id},
    )
    row = cursor.fetchone()
    conn.commit()
    return row


def update_custom_task(conn: psycopg.Connection, id_: int, user_id: int | None, **fields) -> bool:
    """Updates any of title/due_date/status/my_trademark_id. Returns False
    if not found or not owned by this user (caller should 404), or if
    my_trademark_id is being changed to a row not owned by this user (same
    cross-tenant-link concern as create_custom_task — see its docstring)."""
    allowed = {"title", "due_date", "status", "my_trademark_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if "status" in updates and updates["status"] not in CUSTOM_TASK_STATUSES:
        raise ValueError(f"Invalid status: {updates['status']!r}")
    if updates.get("my_trademark_id") is not None and get_trademark(conn, updates["my_trademark_id"], user_id) is None:
        return False
    set_clause = ", ".join(f"{k} = %({k})s" for k in updates)
    params = {**updates, "id": id_, "user_id": user_id}
    cursor = conn.execute(
        f"""
        UPDATE custom_tasks SET {set_clause}, updated_at = now()
        WHERE id = %(id)s AND (%(user_id)s::int IS NULL OR user_id = %(user_id)s)
        """,
        params,
    )
    conn.commit()
    return cursor.rowcount > 0


def delete_custom_task(conn: psycopg.Connection, id_: int, user_id: int | None) -> bool:
    cursor = conn.execute(
        """
        DELETE FROM custom_tasks
        WHERE id = %(id)s AND (%(user_id)s::int IS NULL OR user_id = %(user_id)s)
        """,
        {"id": id_, "user_id": user_id},
    )
    conn.commit()
    return cursor.rowcount > 0


_COMPLIANCE_SOURCES = ("reminder", "correspondence")


def set_compliance_status(conn: psycopg.Connection, source: str, source_id: int, user_id: int, status: str) -> bool:
    """Set *user_id*'s own status on one registry notice (a reminder or
    correspondence row — see COMPLIANCE_STATUS_SCHEMA). Upsert: the first
    time a user touches a given notice this inserts a row, every time after
    that it just updates it. Requires a real user_id (unlike the read-side
    functions here, there's no admin/"set for everyone" concept for a status
    that's inherently personal — same as custom_tasks).

    Returns False (caller should 404) if source_id doesn't resolve to an
    app_no this user actually tracks. ipindia_reminders/ipindia_correspondence
    are shared/global tables keyed by app_no, not by user — without this
    check, any logged-in user could set a status against ANY notice on ANY
    application, not just ones in their own portfolio (no data is disclosed
    by doing so, since list_reminders/list_correspondence only ever join
    compliance_status back for app_nos the caller already tracks, but it's
    still an unintended write against a resource the user has no
    relationship to)."""
    if source == 'event' and config.FLEET_CONTROL_ENABLED:
        from tm_manager.fleet_compliance import update_event
        return update_event(conn, source_id, user_id, {'status': status})
    if source not in _COMPLIANCE_SOURCES:
        raise ValueError(f"Invalid source: {source!r}")
    if status not in CUSTOM_TASK_STATUSES:
        raise ValueError(f"Invalid status: {status!r}")
    table = "ipindia_reminders" if source == "reminder" else "ipindia_correspondence"
    owns = conn.execute(
        f"""
        SELECT 1 FROM {table} src
        JOIN my_trademarks t ON t.app_no = src.app_no AND t.user_id = %(user_id)s
        WHERE src.id = %(source_id)s
        LIMIT 1
        """,
        {"source_id": source_id, "user_id": user_id},
    ).fetchone()
    if not owns:
        return False
    conn.execute(
        """
        INSERT INTO compliance_status (source, source_id, user_id, status)
        VALUES (%(source)s, %(source_id)s, %(user_id)s, %(status)s)
        ON CONFLICT (source, source_id, user_id) DO UPDATE
            SET status = EXCLUDED.status, updated_at = now()
        """,
        {"source": source, "source_id": source_id, "user_id": user_id, "status": status},
    )
    conn.commit()
    return True


_EXAM_REPORT_DATE_RE = re.compile(r"issued on:?\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)

# Internal search-log rows the scraper/portal sometimes leaves behind in
# these tables (not real correspondence) - never surface these as a
# compliance item.
_NOISE_SUBJECT_RE = re.compile(r"^\s*To\s*:\s*None\s*Conditions", re.IGNORECASE)

# Best-effort day offsets for computing a due date when the portal itself
# doesn't publish one - these are NOT official portal deadlines, just this
# app's own estimate from common Trade Marks Act/Rules practice, so every
# date computed this way must be flagged is_estimated=True and shown as
# "(Est.)" wherever it's rendered.
# Deadlines require document facts or a user's confirmation. A reminder's
# despatch date is neither a receipt date nor a scheduled hearing date.


def _classify_compliance_subject(subject: str | None) -> str | None:
    if not subject:
        return None
    s = subject.lower()
    if "u/s 132" in s or "धारा 132" in subject:
        return "reply_us_132"
    if "counter statement" in s:
        return "counter_statement"
    if "show cause" in s or "hearing notice" in s or "hearing (" in s:
        return "hearing_show_cause"
    if "examination report" in s:
        return "examination_report_reply"
    return None


def estimate_notice_deadlines(conn, user_id, start, end, skip_keys=frozenset()):
    """Calendar entries for reminder/correspondence notices that have no stored deadline: the notice's
    despatch (or correspondence) date plus the usual period, always flagged is_estimated. A hearing notice's
    own date is the hearing date. skip_keys holds (app_no, category) pairs that already have a dated event."""
    today = datetime.now().date()
    buckets: dict[str, list[dict]] = {}
    for row in [*list_reminders(conn, user_id, limit=5000), *list_correspondence(conn, user_id, limit=5000)]:
        subject = row.get("subject")
        if not subject or _NOISE_SUBJECT_RE.match(subject):
            continue
        category = _classify_compliance_subject(subject)
        if category is None or (row.get("app_no"), category) in skip_keys:
            continue
        notice_date = _parse_ddmmyyyy(row.get("despatch_date")) or _parse_ddmmyyyy(row.get("corres_date"))
        is_estimated, due_date = True, None
        if category == "examination_report_reply":
            m = _EXAM_REPORT_DATE_RE.search(subject)
            base = (_parse_ddmmyyyy(m.group(1)) if m else None) or notice_date
            due_date = base + timedelta(days=30) if base else None
        elif category == "hearing_show_cause":
            due_date, is_estimated = notice_date, False
        elif notice_date:
            due_date = notice_date + timedelta(days={"reply_us_132": 30, "counter_statement": 60}[category])
        if due_date is None or not (start <= due_date <= end):
            continue
        buckets.setdefault(category, []).append({
            "tm_id": row.get("tm_id"), "app_no": row.get("app_no"), "word_mark": row.get("word_mark"),
            "label": row.get("label"), "has_logo": bool(row.get("has_logo")), "subject": subject,
            "due_date": due_date.isoformat(), "is_estimated": is_estimated, "days_until": (due_date - today).days,
            "source": row.get("source"), "source_id": row.get("source_id"), "status": row.get("status", "pending"),
        })
    return buckets


def list_compliance_notices(
    conn: psycopg.Connection, user_id: int | None, days_ahead: int = 30, days_back: int = 60,
    exclude_done: bool = False,
) -> dict[str, list[dict]]:
    """Groups real reminder/correspondence notices into 4 compliance
    categories by matching real subject text (see _classify_compliance_subject),
    with a computed due date - either a real date embedded in the notice
    (the "Examination Report issued on: DD/MM/YYYY" text, or an actual
    hearing date) or, where no real date is published, despatch/corres date
    plus a best-effort offset (see _DUE_DATE_OFFSET_DAYS), always flagged
    is_estimated so callers never present it as an official deadline.
    Returns only items within [today - days_back, today + days_ahead] so
    this single pass covers "due soon" and "already overdue" (negative
    days_until) - the digest email uses a tight 60-day look-back, while a
    browsable calendar page should pass a much larger days_back/days_ahead
    so navigating to any past/future month still shows real history.

    exclude_done: skip any item the user has marked 'completed' or 'closed'
    (see compliance_status / set_compliance_status). False (default) keeps
    every item regardless of status — the Tasks & Compliances calendar and a
    trademark's own Tasks tab both want the full history, strikethrough and
    all, so a user can see what they already handled. The daily digest
    passes True instead, since there's no reason to keep emailing someone
    about a reply they already marked done."""
    if config.FLEET_CONTROL_ENABLED:
        from tm_manager.fleet_compliance import list_events
        today = datetime.now().date()
        page = list_events(conn, user_id, today - timedelta(days=days_back), today + timedelta(days=days_ahead),
                           exclude_done=exclude_done, include_review=not exclude_done)
        buckets = {}
        for event in page['items']:
            buckets.setdefault(event['category'], []).append(event)
        return buckets
    reminders = list_reminders(conn, user_id, limit=1000)
    correspondence = list_correspondence(conn, user_id, limit=1000)

    today = datetime.now().date()
    horizon = today + timedelta(days=days_ahead)
    overdue_floor = today - timedelta(days=days_back)

    buckets: dict[str, list[dict]] = {
        "examination_report_reply": [],
        "reply_us_132": [],
        "hearing_show_cause": [],
        "counter_statement": [],
    }

    for row in [*reminders, *correspondence]:
        subject = row.get("subject")
        if not subject or _NOISE_SUBJECT_RE.match(subject):
            continue
        category = _classify_compliance_subject(subject)
        if category is None:
            continue
        if exclude_done and row.get("status") in ("completed", "closed"):
            continue

        notice_date = _parse_ddmmyyyy(row.get("despatch_date")) or _parse_ddmmyyyy(row.get("corres_date"))
        is_estimated = True
        due_date = None

        # Legacy records lack verified document anchors; expose them for review
        # without calculating a fictional deadline from the letter date.
        from compliance_facts import events_from_text, parse_date
        facts = events_from_text(subject, row)
        if facts:
            due_date = parse_date(facts[0].get('due_date'))
            is_estimated = facts[0]['due_basis'] != 'stated'

        if (due_date is None and exclude_done) or (due_date is not None and not (overdue_floor <= due_date <= horizon)):
            continue

        buckets[category].append({
            "tm_id": row.get("tm_id"),
            "app_no": row.get("app_no"),
            "word_mark": row.get("word_mark"),
            "label": row.get("label"),
            "has_logo": bool(row.get("has_logo")),
            "subject": subject,
            "due_date": due_date.isoformat() if due_date else None,
            "needs_review": due_date is None or is_estimated,
            "due_basis": facts[0]['due_basis'] if facts else 'unknown',
            "is_estimated": is_estimated,
            "days_until": (due_date - today).days if due_date else None,
            # source/source_id/status: carried straight from list_reminders/
            # list_correspondence's own compliance_status join (see there) —
            # lets a caller PATCH this exact notice's status via
            # set_compliance_status without needing to re-derive which
            # underlying table it came from.
            "source": row.get("source"),
            "source_id": row.get("source_id"),
            "status": row.get("status", "pending"),
        })

    for items in buckets.values():
        items.sort(key=lambda r: r["due_date"] or '')
    return buckets


def list_reminders(conn: psycopg.Connection, user_id: int | None, limit: int = 200) -> list[dict]:
    """Every portal reminder notice (MIS-R/MIS-F, 'Reminder to file...') on
    file for a portfolio trademark, newest fetch first - the closest thing
    to a compliance/deadline log this app has, since IP India only publishes
    a reminder AFTER something is already due, not a forward-looking
    calendar with due dates to compute. Joins my_trademarks so unrelated
    apps' reminders never leak in, and (via user_id) so other tenants'
    reminders never leak in either.

    Also LEFT JOINs compliance_status for *this* user's own status on this
    exact notice (source='reminder', source_id=r.id) — defaults to 'pending'
    when nobody has ever set one, same default a custom_tasks row is born
    with. See set_compliance_status."""
    cursor = conn.execute(
        """
        SELECT r.app_no, r.subject, r.corres_date, r.despatch_date, r.document_path,
               t.id AS tm_id, t.label,
               'reminder' AS source, r.id AS source_id,
               COALESCE(cs.status, 'pending') AS status,
               (res.data_json -> 0 ->> 'Trade Mark') AS word_mark,
               (tma.logo_path IS NOT NULL OR EXISTS (SELECT 1 FROM ipindia_trademark_images ti WHERE ti.app_no = t.app_no)) AS has_logo
        FROM ipindia_reminders r
        JOIN my_trademarks t ON t.app_no = r.app_no
        LEFT JOIN ipindia_results res ON res.id = t.last_search_id
        LEFT JOIN LATERAL (
            SELECT logo_path FROM tma_documents
            WHERE app_no = r.app_no ORDER BY fleet_canonical DESC, extracted_at DESC LIMIT 1
        ) tma ON true
        LEFT JOIN compliance_status cs
            ON cs.source = 'reminder' AND cs.source_id = r.id AND cs.user_id = t.user_id
        WHERE (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
        ORDER BY r.fetched_at DESC
        LIMIT %(limit)s
        """,
        {"limit": limit, "user_id": user_id},
    )
    rows = cursor.fetchall()
    for row in rows:
        row["document_path"] = _strip_notice_folder_prefix(row.get("document_path"))
    return rows


def _strip_notice_folder_prefix(path: str | None) -> str | None:
    """document_path is sometimes saved as a bare filename and sometimes
    with a 'reminders\\'/'correspondence\\' (or '/') folder prefix already
    baked in, depending on when/how it was written - normalize to always be
    a bare filename here, since /trademark/<id>/notice/<kind>/<filename>
    (app.py) already re-adds the kind folder itself. Without this, a
    prefixed path gets double-joined into a path that never exists on disk,
    404ing when the correspondence/reminder link is clicked."""
    if not path:
        return path
    return re.split(r"[\\/]", path)[-1]


def list_correspondence(conn: psycopg.Connection, user_id: int | None, limit: int = 200) -> list[dict]:
    """Every portal correspondence/document entry on file for a portfolio
    trademark, most recent despatch/correspondence date first - hearing
    notices, examination reports, orders, etc. Same join pattern as
    list_reminders, including the same per-user compliance_status join."""
    cursor = conn.execute(
        """
        SELECT c.app_no, c.subject, c.corres_date, c.despatch_date, c.document_path,
               t.id AS tm_id, t.label,
               'correspondence' AS source, c.id AS source_id,
               COALESCE(cs.status, 'pending') AS status,
               (res.data_json -> 0 ->> 'Trade Mark') AS word_mark,
               (tma.logo_path IS NOT NULL OR EXISTS (SELECT 1 FROM ipindia_trademark_images ti WHERE ti.app_no = t.app_no)) AS has_logo
        FROM ipindia_correspondence c
        JOIN my_trademarks t ON t.app_no = c.app_no
        LEFT JOIN ipindia_results res ON res.id = t.last_search_id
        LEFT JOIN LATERAL (
            SELECT logo_path FROM tma_documents
            WHERE app_no = c.app_no ORDER BY fleet_canonical DESC, extracted_at DESC LIMIT 1
        ) tma ON true
        LEFT JOIN compliance_status cs
            ON cs.source = 'correspondence' AND cs.source_id = c.id AND cs.user_id = t.user_id
        WHERE (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
        ORDER BY
            to_date(NULLIF(COALESCE(c.despatch_date, c.corres_date), ''), 'DD/MM/YYYY') DESC NULLS LAST,
            c.fetched_at DESC
        LIMIT %(limit)s
        """,
        {"limit": limit, "user_id": user_id},
    )
    rows = cursor.fetchall()
    for row in rows:
        row["document_path"] = _strip_notice_folder_prefix(row.get("document_path"))
    return rows


def list_journal_matches(
    conn: psycopg.Connection, user_id: int | None, min_similarity: float = 0.55, journal_no: str | None = None
) -> list[dict]:
    """Every precomputed journal-entry match for a portfolio trademark,
    highest similarity first - populated by scripts/parse_journal.py, not
    computed live (see that script's module docstring for why). Empty until
    that script has been run at least once against a journal PDF.
    journal_no restricts to one specific journal (for viewing a past one);
    omitted, every parsed journal's matches come back together. journal_entries
    itself stays global (public IP India journal data); user_id scopes which
    of YOUR OWN portfolio marks' matches you see."""
    cursor = conn.execute(
        """
        SELECT
            m.similarity,
            t.id AS tm_id, t.app_no AS tm_app_no, t.label AS tm_label,
            (r.data_json -> 0 ->> 'Trade Mark') AS tm_word_mark,
            (r.data_json -> 0 ->> 'Status') AS tm_status,
            COALESCE(tma.class_number, r.data_json -> 0 ->> 'Class') AS tm_class_number,
            (tma.logo_path IS NOT NULL OR EXISTS (SELECT 1 FROM ipindia_trademark_images ti WHERE ti.app_no = t.app_no)) AS tm_has_logo,
            je.id AS entry_id, je.journal_no, je.journal_date, je.app_no AS entry_app_no,
            je.app_date AS entry_app_date, je.class_number, je.word_mark AS entry_word_mark,
            je.applicant_name AS entry_applicant_name,
            je.applicant_address AS entry_applicant_address, je.page_number
        FROM journal_matches m
        JOIN my_trademarks t ON t.id = m.my_trademark_id
        JOIN journal_entries je ON je.id = m.journal_entry_id
        LEFT JOIN ipindia_results r ON r.id = t.last_search_id
        LEFT JOIN LATERAL (
            SELECT class_number, logo_path FROM tma_documents
            WHERE app_no = t.app_no
            ORDER BY fleet_canonical DESC, extracted_at DESC
            LIMIT 1
        ) tma ON true
        WHERE m.similarity >= %(min_similarity)s
          AND (%(journal_no)s::text IS NULL OR je.journal_no = %(journal_no)s::text)
          AND (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
        ORDER BY m.similarity DESC
        """,
        {"min_similarity": min_similarity, "journal_no": journal_no, "user_id": user_id},
    )
    return cursor.fetchall()


def latest_journal_no(conn: psycopg.Connection) -> str | None:
    cursor = conn.execute("SELECT journal_no FROM journal_entries ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    return row["journal_no"] if row else None


def list_journal_numbers(conn: psycopg.Connection) -> list[dict]:
    """Every journal that's been parsed, newest publication date first,
    with its entry count — for a "past journals" list in the UI. IP India
    publishes weekly, each week's journal split across several class-range
    PDFs (Class 1-9, 10-25, ...) that all share the same journal_no and
    journal_date — every part gets uploaded under that same journal_no
    (see parse_journal.py's ON CONFLICT upsert), so grouping by journal_no
    here already collapses those parts back into one row per week. Sorted
    by the actual DD/MM/YYYY date rather than journal_no's text order,
    since a numbering gap or reissue shouldn't silently reorder the list."""
    cursor = conn.execute(
        """
        SELECT journal_no, journal_date, count(*) AS n
        FROM journal_entries
        GROUP BY journal_no, journal_date
        ORDER BY 
            CASE WHEN journal_date LIKE '%/%/%' THEN to_date(journal_date, 'DD/MM/YYYY')
            ELSE journal_date::date END DESC NULLS LAST
        """
    )
    return cursor.fetchall()


def create_journal_job(
    conn: psycopg.Connection,
    filename: str,
    total_files: int = 1,
    journal_no: str | None = None,
    journal_date: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO journal_jobs (filename, total_files, journal_no, journal_date)
        VALUES (%(filename)s, %(total_files)s, %(journal_no)s, %(journal_date)s)
        RETURNING id
        """,
        {"filename": filename, "total_files": total_files, "journal_no": journal_no, "journal_date": journal_date},
    )
    conn.commit()
    return cursor.fetchone()["id"]


def update_journal_job(conn: psycopg.Connection, job_id: int, **fields) -> None:
    """Patches whichever of status/journal_no/total_pages/pages_done/
    entries_found/matches_found/error were passed — scripts/parse_journal.py
    calls this repeatedly as it works through the PDF so the frontend has
    something to poll."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %({k})s" for k in fields)
    conn.execute(
        f"UPDATE journal_jobs SET {set_clause}, updated_at = now() WHERE id = %(id)s",
        {**fields, "id": job_id},
    )
    conn.commit()


def get_journal_job(conn: psycopg.Connection, job_id: int) -> dict | None:
    cursor = conn.execute("SELECT * FROM journal_jobs WHERE id = %(id)s", {"id": job_id})
    return cursor.fetchone()


def get_latest_journal_job(conn: psycopg.Connection) -> dict | None:
    cursor = conn.execute("SELECT * FROM journal_jobs ORDER BY id DESC LIMIT 1")
    return cursor.fetchone()


_MAX_AUTO_RETRY_ATTEMPTS = 5


def claim_next_pending_trademark(conn: psycopg.Connection) -> dict | None:
    """
    Atomically claim the next trademark to fetch - prefers a 'pending' row
    (newest additions first get worked off) but also picks up 'error' rows
    for automatic retry, deprioritized behind fresh pending ones and capped
    at _MAX_AUTO_RETRY_ATTEMPTS (a row that keeps failing before a scrape
    job even starts - e.g. a wedged pool-account login - would otherwise
    win the "lowest attempt_count" tiebreaker forever and starve every
    other queued trademark behind it; once it hits the cap it's left in
    status='error' for a human to retry manually via the Refetch button).
    Mirrors the scraper app's own claim_free_pool_account/claim_next_series
    pattern: a single UPDATE...WHERE...RETURNING statement, no separate
    SELECT-then-UPDATE race window.

    attempt_count is incremented HERE, at claim time - not only once a
    scrape job successfully starts - so a row that fails during login
    (before any job exists) still counts as an attempt and doesn't get
    reselected on every single tick.
    """
    cursor = conn.execute(
        """
        UPDATE my_trademarks
        SET status = 'fetching', attempt_count = attempt_count + 1,
            last_attempted_at = now(), updated_at = now()
        WHERE id = (
            SELECT id FROM my_trademarks
            WHERE (status = 'pending')
               OR (status = 'error' AND attempt_count < %(max_attempts)s)
            -- fresh 'pending' first; within that, HIGHEST priority, then
            -- NEWEST added (a mark you just added in the UI has priority=100
            -- and jumps the migrated backlog which is priority=0).
            ORDER BY (status = 'error'), priority DESC, created_at DESC, attempt_count
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        {"max_attempts": _MAX_AUTO_RETRY_ATTEMPTS},
    )
    row = cursor.fetchone()
    conn.commit()
    return row


def claim_batch_pending_trademarks(conn: psycopg.Connection, batch_size: int) -> list[dict]:
    """Same selection rule as claim_next_pending_trademark, but claims up to
    `batch_size` rows in one statement — feeds /api/scrape-appno-batch on the
    scraper, which holds ONE session for the whole batch (like Auto Discover)
    instead of the old one-HTTP-job-per-mark pattern. FOR UPDATE SKIP LOCKED
    still makes this safe if more than one worker calls it concurrently."""
    cursor = conn.execute(
        """
        UPDATE my_trademarks
        SET status = 'fetching', attempt_count = attempt_count + 1,
            last_attempted_at = now(), updated_at = now()
        WHERE id IN (
            SELECT id FROM my_trademarks
            WHERE (status = 'pending')
               OR (status = 'error' AND attempt_count < %(max_attempts)s)
            ORDER BY (status = 'error'), priority DESC, created_at DESC, attempt_count
            LIMIT %(batch_size)s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        {"max_attempts": _MAX_AUTO_RETRY_ATTEMPTS, "batch_size": batch_size},
    )
    rows = cursor.fetchall()
    conn.commit()
    return rows


def list_due_trademarks(conn: psycopg.Connection) -> list[dict]:
    """Read-only sibling of claim_batch_pending_trademarks — EXACT same
    WHERE/ORDER BY (same 'due' definition: fresh 'pending' rows first, then
    'error' rows still under the retry cap, priority DESC, newest first,
    lowest attempt_count as the final tiebreaker) but a plain SELECT, no
    UPDATE, no FOR UPDATE SKIP LOCKED. Used ONLY by the fleet coordinator's
    daily planning (plan_daily_fleet_assignments) to see the full "due
    today" pool across every client BEFORE deciding which machine gets
    which rows — it must never flip status to 'fetching' itself, since a
    row isn't actually claimed until a worker machine calls
    claim_fleet_assigned_batch against its own fleet_assignments slice.
    Not scoped by user_id — planning is deliberately flat across every
    client's marks combined (see the fleet plan doc's "not per-client"
    instruction), the machine-fair split happens entirely in
    plan_daily_fleet_assignments, not here."""
    cursor = conn.execute(
        """
        SELECT * FROM my_trademarks
        WHERE (status = 'pending')
           OR (status = 'error' AND attempt_count < %(max_attempts)s)
        ORDER BY (status = 'error'), priority DESC, created_at DESC, attempt_count
        """,
        {"max_attempts": _MAX_AUTO_RETRY_ATTEMPTS},
    )
    return cursor.fetchall()


def requeue_stale_trademarks(conn: psycopg.Connection, interval_days: int, batch: int) -> int:
    """Move up to `batch` marks that were last fetched more than
    `interval_days` ago from 'fetched' back to 'pending', so the auto-fetch
    worker refreshes them (and records a new tm_status_history snapshot,
    capturing any status change). Oldest-fetched first. Returns how many
    were re-queued."""
    cursor = conn.execute(
        """
        UPDATE my_trademarks
        SET status = 'pending', attempt_count = 0, updated_at = now()
        WHERE id IN (
            SELECT id FROM my_trademarks
            WHERE status = 'fetched'
              AND fetched_at IS NOT NULL
              AND fetched_at < now() - make_interval(days => %(d)s)
            ORDER BY fetched_at ASC
            LIMIT %(b)s
            FOR UPDATE SKIP LOCKED
        )
        """,
        {"d": interval_days, "b": batch},
    )
    conn.commit()
    return cursor.rowcount


def count_error_trademarks(conn: psycopg.Connection, user_id: int | None) -> int:
    return conn.execute(
        """
        SELECT count(*) AS n FROM my_trademarks
        WHERE status = 'error' AND (%(user_id)s::int IS NULL OR user_id = %(user_id)s)
        """,
        {"user_id": user_id},
    ).fetchone()["n"]


def mark_trademark_job_started(conn: psycopg.Connection, id_: int, job_id: str) -> None:
    """Records which scrape job is now handling this trademark. attempt_count
    is already incremented at claim time (claim_next_pending_trademark) - not
    incremented again here, so a claim that fails before this point (e.g.
    login failure) still counted as exactly one attempt, not zero."""
    conn.execute(
        """
        UPDATE my_trademarks
        SET job_id = %(job_id)s, updated_at = now()
        WHERE id = %(id)s
        """,
        {"id": id_, "job_id": job_id},
    )
    conn.commit()


# Fields that must actually be present (from the status-page scrape, the
# parsed TM-A/TM-1 PDF, or either) before a fetch counts as complete. A hit
# that's missing these is a partial portal parse — see
# has_required_trademark_fields — not a real "we have this trademark's
# data" result, even though the portal itself returned a match.
_REQUIRED_FIELD_LABELS = ("Application Date", "Date of Application", "Class", "State")


def has_required_trademark_fields(conn: psycopg.Connection, app_no: str, search_id: int) -> bool:
    """True if, between the status-page scrape (ipindia_results.data_json)
    and the latest parsed application-form PDF (tma_documents), the
    important fields are actually present — application date and class at
    minimum, since those are what every trademark record needs to be useful.
    A "hit" with a matching portal record but blank fields (e.g. the portal
    rendered a narrower results table for this app_no's filing history) is
    NOT good enough to call 'fetched' — see mark_trademark_fetched."""
    res = conn.execute(
        "SELECT data_json FROM ipindia_results WHERE id = %s", (search_id,)
    ).fetchone()
    data = (res["data_json"][0] if res and res.get("data_json") else {}) or {}

    tma = conn.execute(
        "SELECT application_date, class_number FROM tma_documents "
        "WHERE app_no = %(app_no)s ORDER BY fleet_canonical DESC, extracted_at DESC LIMIT 1",
        {"app_no": app_no},
    ).fetchone() or {}

    has_date = bool(data.get("Application Date") or data.get("Date of Application") or tma.get("application_date"))
    has_class = bool(data.get("Class") or tma.get("class_number"))
    return has_date and has_class


def mark_trademark_fetched(conn: psycopg.Connection, id_: int, search_id: int) -> None:
    conn.execute(
        """
        UPDATE my_trademarks
        SET status = 'fetched', last_search_id = %(search_id)s,
            fetched_at = now(), job_id = NULL, last_error = NULL, updated_at = now()
        WHERE id = %(id)s
        """,
        {"id": id_, "search_id": search_id},
    )
    conn.commit()
    # Append a status-history snapshot + diff. Never let a history-write
    # failure fail the fetch itself.
    try:
        record_status_snapshot(conn, id_, search_id)
    except Exception:
        import logging as _log
        _log.getLogger(__name__).exception("record_status_snapshot failed for tm %s", id_)


# ── Status-history ledger (previous vs current status, full timeline) ────────

# The portal fields we snapshot + diff on each fetch. Keys are the labels IP
# India uses inside ipindia_results.data_json[0]; values are the
# tm_status_history column names.
_TSH_FIELDS = {
    "Status": "status",
    "Sub Status": "sub_status",
    "Trade Mark": "trade_mark",
    "Proprietor Name": "proprietor_name",
    "Class": "class_number",
    "Valid Upto/Renewed Upto": "valid_upto",
    "Publication Details": "publication_details",
    "TM Type": "tm_type",
}


def record_status_snapshot(conn: psycopg.Connection, my_trademark_id: int, search_id: int) -> dict | None:
    """Read the portal payload for `search_id`, diff it against this mark's
    previous snapshot, and insert a tm_status_history row. Returns the
    inserted row's changed_fields dict (or None if nothing was inserted)."""
    from psycopg.types.json import Jsonb

    app_row = conn.execute(
        "SELECT app_no FROM my_trademarks WHERE id = %s", (my_trademark_id,)
    ).fetchone()
    if not app_row:
        return None
    app_no = app_row["app_no"]

    res = conn.execute(
        "SELECT data_json, scraped_at FROM ipindia_results WHERE id = %s", (search_id,)
    ).fetchone()
    if not res or not res.get("data_json"):
        return None
    payload = (res["data_json"][0] if res["data_json"] else {}) or {}
    fetched_at = res["scraped_at"]

    snap = {col: (payload.get(label) or None) for label, col in _TSH_FIELDS.items()}

    prev = conn.execute(
        """
        SELECT status, sub_status, trade_mark, proprietor_name, class_number,
               valid_upto, publication_details, tm_type
        FROM tm_status_history
        WHERE my_trademark_id = %s
        ORDER BY fetched_at DESC, id DESC
        LIMIT 1
        """,
        (my_trademark_id,),
    ).fetchone()

    is_first = prev is None
    changed: dict = {}
    if not is_first:
        for col in snap:
            old_v = prev.get(col)
            new_v = snap.get(col)
            if (old_v or None) != (new_v or None):
                changed[col] = {"from": old_v, "to": new_v}

    cur = conn.execute(
        """
        INSERT INTO tm_status_history
            (my_trademark_id, app_no, search_id, fetched_at,
             status, sub_status, trade_mark, proprietor_name, class_number,
             valid_upto, publication_details, tm_type, changed_fields, is_first)
        VALUES
            (%(tm)s, %(app_no)s, %(sid)s, %(fetched_at)s,
             %(status)s, %(sub_status)s, %(trade_mark)s, %(proprietor_name)s, %(class_number)s,
             %(valid_upto)s, %(publication_details)s, %(tm_type)s, %(changed)s, %(is_first)s)
        ON CONFLICT (my_trademark_id, search_id) WHERE search_id IS NOT NULL DO NOTHING
        RETURNING changed_fields
        """,
        {
            "tm": my_trademark_id, "app_no": app_no, "sid": search_id, "fetched_at": fetched_at,
            "changed": Jsonb(changed), "is_first": is_first, **snap,
        },
    ).fetchone()
    conn.commit()
    return cur["changed_fields"] if cur else None


def list_status_history(conn: psycopg.Connection, my_trademark_id: int, user_id: int | None) -> list[dict]:
    """Full fetch timeline for one mark, oldest first. Joins my_trademarks
    for the ownership check (None = admin, bypasses it) — callers should
    already have resolved the trademark via get_trademark first, but this
    guards independently rather than trusting a bare id alone."""
    rows = conn.execute(
        """
        SELECT h.id, h.search_id, h.fetched_at, h.status, h.sub_status, h.trade_mark,
               h.proprietor_name, h.class_number, h.valid_upto, h.publication_details,
               h.tm_type, h.changed_fields, h.is_first
        FROM tm_status_history h
        JOIN my_trademarks t ON t.id = h.my_trademark_id
        WHERE h.my_trademark_id = %(id)s
          AND (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
        ORDER BY h.fetched_at ASC, h.id ASC
        """,
        {"id": my_trademark_id, "user_id": user_id},
    ).fetchall()
    return rows


def prev_and_current_status(conn: psycopg.Connection, my_trademark_id: int, user_id: int | None) -> dict:
    """The two most recent snapshots -> {previous, current, changed_at}."""
    rows = conn.execute(
        """
        SELECT h.status, h.fetched_at
        FROM tm_status_history h
        JOIN my_trademarks t ON t.id = h.my_trademark_id
        WHERE h.my_trademark_id = %(id)s
          AND (%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)
        ORDER BY h.fetched_at DESC, h.id DESC
        LIMIT 2
        """,
        {"id": my_trademark_id, "user_id": user_id},
    ).fetchall()
    current = rows[0]["status"] if rows else None
    previous = rows[1]["status"] if len(rows) > 1 else None
    changed_at = rows[0]["fetched_at"] if rows else None
    return {
        "previous": previous,
        "current": current,
        "changed_at": changed_at,
        "changed": bool(previous is not None and previous != current),
    }


def list_portfolio_changes(
    conn: psycopg.Connection, user_id: int | None, since=None, field: str = "status", limit: int = 200,
) -> list[dict]:
    """Every change of `field` across the whole portfolio, newest first.
    `since` (a date/datetime) bounds it; `field` is a tm_status_history
    column name (default 'status'). user_id: None (admin) sees every
    tenant's changes; a real id sees only that user's own."""
    if field not in _TSH_FIELDS.values():
        field = "status"
    params: dict = {"limit": limit, "field": field, "user_id": user_id}
    where = ["h.changed_fields ? %(field)s", "h.is_first = false",
             "(h.changed_fields -> %(field)s ->> 'from') IS NOT NULL",
             "(%(user_id)s::int IS NULL OR t.user_id = %(user_id)s)"]
    if since is not None:
        where.append("h.fetched_at >= %(since)s")
        params["since"] = since
    rows = conn.execute(
        f"""
        SELECT h.id, h.my_trademark_id, h.app_no, h.fetched_at,
               h.changed_fields -> %(field)s ->> 'from' AS old_value,
               h.changed_fields -> %(field)s ->> 'to'   AS new_value,
               t.label, h.trade_mark AS word_mark, h.proprietor_name
        FROM tm_status_history h
        JOIN my_trademarks t ON t.id = h.my_trademark_id
        WHERE {' AND '.join(where)}
        ORDER BY h.fetched_at DESC, h.id DESC
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    return rows


def requeue_old_errors(conn: psycopg.Connection, older_than_hours: int, batch: int) -> int:
    """Give up-for-good 'error' rows (attempt_count hit _MAX_AUTO_RETRY_ATTEMPTS,
    so claim_batch_pending_trademarks/claim_next_pending_trademark will never
    select them again) a fresh retry budget once they've sat untouched for a
    while. Without this, any mark that failed 5 times during a rough patch
    (e.g. only 1 of 2 warm accounts up) stays stuck in 'error' forever with
    nothing but a manual Refetch click to save it — that's exactly what
    produced a 687-row backlog stuck for 3+ days on 2026-09-14. Oldest
    last_attempted_at first. Returns how many were re-queued."""
    cursor = conn.execute(
        """
        UPDATE my_trademarks
        SET status = 'pending', job_id = NULL, attempt_count = 0, updated_at = now()
        WHERE id IN (
            SELECT id FROM my_trademarks
            WHERE status = 'error'
              AND last_attempted_at IS NOT NULL
              AND last_attempted_at < now() - make_interval(hours => %(h)s)
            ORDER BY last_attempted_at ASC
            LIMIT %(b)s
            FOR UPDATE SKIP LOCKED
        )
        """,
        {"h": older_than_hours, "b": batch},
    )
    conn.commit()
    return cursor.rowcount


def mark_trademark_error(conn: psycopg.Connection, id_: int, error: str, retryable: bool = False) -> None:
    """Record a failed attempt. When retryable=True (capacity/transient
    failures - no free pool account, unreachable scraper, timed-out poll -
    see scheduler._RETRYABLE_ERROR_MARKERS) attempt_count is rolled back by
    one right after claim_next_pending_trademark incremented it, so these
    never count against the 5-attempt auto-retry budget: the row would
    otherwise sit in 'error' forever once the cap is hit even though the
    underlying cause (e.g. the shared OTP pool being busy) has nothing to do
    with this specific application number and clears up on its own."""
    conn.execute(
        """
        UPDATE my_trademarks
        SET status = 'error', last_error = %(error)s, job_id = NULL, updated_at = now(),
            attempt_count = GREATEST(0, attempt_count - %(rollback)s)
        WHERE id = %(id)s
        """,
        {"id": id_, "error": error, "rollback": 1 if retryable else 0},
    )
    conn.commit()


def retry_all_errors(conn: psycopg.Connection) -> int:
    """Bulk reset every 'error' row back to 'pending' for the scheduler to
    pick up again, same as a manual per-row Refetch click but for the whole
    backlog at once - needed because attempt_count is per-row and a shared
    transient outage (pool exhaustion, scraper app briefly down) can push
    many rows past _MAX_AUTO_RETRY_ATTEMPTS around the same time, each then
    stuck waiting for an individual manual retry."""
    cursor = conn.execute(
        "UPDATE my_trademarks SET status = 'pending', job_id = NULL, attempt_count = 0, updated_at = now() WHERE status = 'error'"
    )
    conn.commit()
    return cursor.rowcount


def reset_trademark_to_pending(conn: psycopg.Connection, id_: int, user_id: int | None) -> bool:
    """Manual retry/refetch - user-triggered reset back to 'pending' so the
    scheduler picks it up again without waiting. Also resets attempt_count
    to 0: a deliberate manual click is a fresh start, not part of the
    automatic retry budget - otherwise a row that already hit the
    automatic-retry cap could never be retried again, even by hand.
    Returns True if a row was updated (False = not found or not owned)."""
    cursor = conn.execute(
        """
        UPDATE my_trademarks
        SET status = 'pending', job_id = NULL, attempt_count = 0, updated_at = now()
        WHERE id = %(id)s
          AND (%(user_id)s::int IS NULL OR user_id = %(user_id)s)
        """,
        {"id": id_, "user_id": user_id},
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_trademark_fetching(conn: psycopg.Connection, id_: int) -> None:
    """Flips a row straight to 'fetching' — used by the Live Refresh fast
    lane (trademark_live_refresh) right before handing it to
    scheduler._run_fast_fetch_one in a background thread, so the frontend's
    existing poll loop (which already watches for status leaving
    pending/fetching) shows the refreshing state immediately, instead of
    'pending' which implies waiting for the slow batch worker to pick it up."""
    conn.execute(
        "UPDATE my_trademarks SET status = 'fetching', updated_at = now() WHERE id = %(id)s",
        {"id": id_},
    )
    conn.commit()


def remove_trademark(conn: psycopg.Connection, id_: int, user_id: int | None) -> bool:
    """Returns True if a row was deleted (False = not found or not owned)."""
    cursor = conn.execute(
        "DELETE FROM my_trademarks WHERE id = %(id)s AND (%(user_id)s::int IS NULL OR user_id = %(user_id)s)",
        {"id": id_, "user_id": user_id},
    )
    conn.commit()
    return cursor.rowcount > 0


def remove_trademarks(conn: psycopg.Connection, ids: list[int], user_id: int | None) -> int:
    """Remove selected tracked portfolio records in one transaction — only
    rows the caller actually owns (None = admin, removes any of the given
    ids); any id in the list that belongs to someone else is silently
    skipped rather than erroring, and the returned count reflects only what
    was actually deleted.

    Historical IP India results and downloaded source files are intentionally
    retained; this only removes the user's portfolio records.
    """
    if not ids:
        return 0
    cursor = conn.execute(
        "DELETE FROM my_trademarks WHERE id = ANY(%(ids)s::int[]) AND (%(user_id)s::int IS NULL OR user_id = %(user_id)s)",
        {"ids": ids, "user_id": user_id},
    )
    conn.commit()
    return cursor.rowcount


def release_stuck_fetching_trademarks(conn: psycopg.Connection) -> int:
    """Startup sweep: any row still 'fetching' when this process starts is
    orphaned (the previous process's in-memory tracking of that fetch is
    gone). Reset to 'pending' - safe, since a duplicate scrape is harmless
    (ipindia_results has no unique constraint to violate), just re-verified
    on the next tick rather than lost forever."""
    cursor = conn.execute(
        "UPDATE my_trademarks SET status = 'pending', job_id = NULL, updated_at = now() WHERE status = 'fetching'"
    )
    conn.commit()
    return cursor.rowcount


# ── Per-user Trademarks table column preferences ────────────────────────────

def get_user_column_prefs(conn: psycopg.Connection, user_id: int) -> list[str] | None:
    """Saved column choice/order for the Trademarks table, or None if this
    user has never customized it (caller falls back to the app's default
    set). App Number is never stored here — it's always shown first."""
    row = conn.execute(
        "SELECT trademarks_column_prefs FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    if not row or not row["trademarks_column_prefs"]:
        return None
    return row["trademarks_column_prefs"].get("columns")


def set_user_column_prefs(conn: psycopg.Connection, user_id: int, columns: list[str]) -> None:
    conn.execute(
        "UPDATE users SET trademarks_column_prefs = %s WHERE id = %s",
        (Jsonb({"columns": columns}), user_id),
    )
    conn.commit()


# ── User management (admin-only routes in app.py) ───────────────────────────

def list_users(conn: psycopg.Connection) -> list[dict]:
    """Every account, with how many trademarks each one owns — admin's user
    management screen. Never returns password_hash."""
    return conn.execute(
        """
        SELECT u.id, u.email, u.display_name, u.is_admin, u.is_active,
               u.created_at, u.last_login_at,
               (SELECT count(*) FROM my_trademarks t WHERE t.user_id = u.id) AS trademark_count
        FROM users u
        ORDER BY u.created_at ASC
        """
    ).fetchall()


def create_user(
    conn: psycopg.Connection, email: str, password_hash: str,
    is_admin: bool = False, display_name: str | None = None,
) -> tuple[int | None, str | None]:
    """Returns (id, None) on success, or (None, "email already in use")."""
    existing = conn.execute(
        "SELECT id FROM users WHERE lower(email) = lower(%s)", (email,)
    ).fetchone()
    if existing:
        return None, "email already in use"
    row = conn.execute(
        """
        INSERT INTO users (email, password_hash, is_admin, display_name)
        VALUES (%(email)s, %(password_hash)s, %(is_admin)s, %(display_name)s)
        RETURNING id
        """,
        {"email": email, "password_hash": password_hash, "is_admin": is_admin, "display_name": display_name},
    ).fetchone()
    conn.commit()
    return row["id"], None


def set_user_active(conn: psycopg.Connection, user_id: int, is_active: bool) -> bool:
    """Deactivate (or reactivate) an account — a deactivated user is caught
    and logged out on their very next request (see app.py's _require_login).
    Returns True if a row was updated."""
    cursor = conn.execute(
        "UPDATE users SET is_active = %s WHERE id = %s", (is_active, user_id)
    )
    conn.commit()
    return cursor.rowcount > 0


def set_user_admin(conn: psycopg.Connection, user_id: int, is_admin: bool) -> bool:
    cursor = conn.execute(
        "UPDATE users SET is_admin = %s WHERE id = %s", (is_admin, user_id)
    )
    conn.commit()
    return cursor.rowcount > 0


def update_user(
    conn: psycopg.Connection, user_id: int, email: str, display_name: str | None,
) -> tuple[bool, str | None]:
    """Update an existing account's email/display name — the admin-UI
    equivalent of scripts/set_email.py, folded into one call alongside
    display_name so "Edit user" is a single save. Returns (True, None) on
    success, (False, "user not found") if user_id doesn't exist, or
    (False, "email already in use") if another account already has that
    email (same case-insensitive uniqueness check create_user/set_email.py
    already use — enforced here too since there's no DB-level constraint
    doing it for us, see USERS_SCHEMA's uq_users_email which IS
    case-insensitive but only raises a raw IntegrityError, not this
    friendlier message)."""
    exists = conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone()
    if not exists:
        return False, "user not found"
    taken = conn.execute(
        "SELECT id FROM users WHERE lower(email) = lower(%s) AND id != %s", (email, user_id),
    ).fetchone()
    if taken:
        return False, "email already in use"
    conn.execute(
        "UPDATE users SET email = %(email)s, display_name = %(display_name)s WHERE id = %(id)s",
        {"email": email, "display_name": display_name, "id": user_id},
    )
    conn.commit()
    return True, None


def set_user_password(conn: psycopg.Connection, user_id: int, password_hash: str) -> bool:
    """Admin-UI equivalent of scripts/set_password.py. Returns True if a
    row was updated (user_id exists)."""
    cursor = conn.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id)
    )
    conn.commit()
    return cursor.rowcount > 0


# ── Subscription/billing (see BILLING_SCHEMA) ────────────────────────────────

def list_plans(conn: psycopg.Connection, include_inactive: bool = True) -> list[dict]:
    """All plan rows, newest first. include_inactive=False is for anywhere
    that offers a plan for NEW selection (e.g. a future self-serve signup
    flow) — a retired plan should still resolve correctly for whoever's
    already on it (see get_effective_limits), just not be offered again."""
    where = "" if include_inactive else "WHERE is_active"
    cursor = conn.execute(f"SELECT * FROM plans {where} ORDER BY id DESC")
    return cursor.fetchall()


def get_plan(conn: psycopg.Connection, plan_id: int) -> dict | None:
    return conn.execute("SELECT * FROM plans WHERE id = %s", (plan_id,)).fetchone()


def create_plan(
    conn: psycopg.Connection, name: str, tm_limit: int, seat_limit: int,
    price_cents: int | None = None, features: dict | None = None,
    search_limit: int | None = None, billing_period_months: int = 1,
) -> int:
    row = conn.execute(
        """
        INSERT INTO plans (name, price_cents, tm_limit, seat_limit, features, search_limit, billing_period_months)
        VALUES (%(name)s, %(price_cents)s, %(tm_limit)s, %(seat_limit)s, %(features)s, %(search_limit)s, %(billing_period_months)s)
        RETURNING id
        """,
        {
            "name": name, "price_cents": price_cents, "tm_limit": tm_limit,
            "seat_limit": seat_limit, "features": Jsonb(features or {}),
            "search_limit": search_limit, "billing_period_months": billing_period_months,
        },
    ).fetchone()
    conn.commit()
    return row["id"]


def update_plan(
    conn: psycopg.Connection, plan_id: int, *, name: str | None = None,
    price_cents: int | None = None, tm_limit: int | None = None,
    seat_limit: int | None = None, features: dict | None = None,
    is_active: bool | None = None,
    search_limit: int | None = None, billing_period_months: int | None = None,
) -> bool:
    """Partial update — only the kwargs actually passed (non-None) are
    changed, same "PATCH, not PUT" shape as update_user. Returns True if
    plan_id existed."""
    fields = {
        "name": name, "price_cents": price_cents, "tm_limit": tm_limit,
        "seat_limit": seat_limit, "is_active": is_active,
        "search_limit": search_limit, "billing_period_months": billing_period_months,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    if features is not None:
        fields["features"] = Jsonb(features)
    if not fields:
        return conn.execute("SELECT id FROM plans WHERE id = %s", (plan_id,)).fetchone() is not None
    fields["updated_at"] = datetime.now()
    set_clause = ", ".join(f"{k} = %({k})s" for k in fields)
    fields["id"] = plan_id
    cursor = conn.execute(f"UPDATE plans SET {set_clause} WHERE id = %(id)s", fields)
    conn.commit()
    return cursor.rowcount > 0


def list_subscriptions(conn: psycopg.Connection) -> list[dict]:
    """Every subscription joined with its plan name and the paying user's
    email/display_name, plus a live count of that account's current
    my_trademarks rows — everything the admin billing page needs in one
    query rather than N+1 round trips per row."""
    cursor = conn.execute(
        """
        SELECT s.*, p.name AS plan_name, p.tm_limit AS plan_tm_limit,
               p.seat_limit AS plan_seat_limit, p.features AS plan_features,
               u.email AS user_email, u.display_name AS user_display_name,
               (SELECT count(*) FROM my_trademarks m WHERE m.user_id = s.user_id) AS tm_count
        FROM subscriptions s
        JOIN plans p ON p.id = s.plan_id
        JOIN users u ON u.id = s.user_id
        ORDER BY s.updated_at DESC
        """
    )
    return cursor.fetchall()


def get_subscription(conn: psycopg.Connection, user_id: int) -> dict | None:
    """The raw subscription row for this exact user_id (must already be a
    paying account, not a seat — callers resolve parent_account_id first,
    see billing.get_effective_limits)."""
    return conn.execute(
        "SELECT * FROM subscriptions WHERE user_id = %s", (user_id,)
    ).fetchone()


def upsert_subscription(
    conn: psycopg.Connection, user_id: int, plan_id: int, *,
    status: str = "active", renews_at: datetime | None = None,
    tm_limit_override: int | None = None, seat_limit_override: int | None = None,
    feature_overrides: dict | None = None, search_limit_override: int | None = None,
) -> int:
    """Create or replace the one subscription row a paying account can have
    (see BILLING_SCHEMA's UNIQUE user_id). This is the single function both
    "assign a brand-new plan" and "edit this account's overrides" go
    through — an admin PATCHing just tm_limit_override still has to pass
    plan_id/status along (the admin route reads the existing row first and
    fills in what wasn't changed), which keeps this function's shape simple
    at the cost of a slightly chattier caller."""
    row = conn.execute(
        """
        INSERT INTO subscriptions
            (user_id, plan_id, status, renews_at, tm_limit_override, seat_limit_override, feature_overrides, search_limit_override, updated_at)
        VALUES
            (%(user_id)s, %(plan_id)s, %(status)s, %(renews_at)s, %(tm_limit_override)s, %(seat_limit_override)s, %(feature_overrides)s, %(search_limit_override)s, now())
        ON CONFLICT (user_id) DO UPDATE SET
            plan_id = EXCLUDED.plan_id,
            status = EXCLUDED.status,
            renews_at = EXCLUDED.renews_at,
            tm_limit_override = EXCLUDED.tm_limit_override,
            seat_limit_override = EXCLUDED.seat_limit_override,
            feature_overrides = EXCLUDED.feature_overrides,
            search_limit_override = EXCLUDED.search_limit_override,
            updated_at = now()
        RETURNING id
        """,
        {
            "user_id": user_id, "plan_id": plan_id, "status": status, "renews_at": renews_at,
            "tm_limit_override": tm_limit_override, "seat_limit_override": seat_limit_override,
            "feature_overrides": Jsonb(feature_overrides) if feature_overrides is not None else None,
            "search_limit_override": search_limit_override,
        },
    ).fetchone()
    conn.commit()
    return row["id"]


def resolve_paying_account_id(conn: psycopg.Connection, user_id: int) -> int:
    """A seat (users.parent_account_id set) is billed against its paying
    account, never itself — this is the one place that distinction is
    resolved, so billing.get_effective_limits and every enforcement call
    site stay one line each instead of re-deriving it. Returns user_id
    unchanged if it has no parent (i.e. it IS the paying account)."""
    row = conn.execute(
        "SELECT parent_account_id FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    if row and row["parent_account_id"]:
        return row["parent_account_id"]
    return user_id


def count_seats(conn: psycopg.Connection, paying_account_id: int) -> int:
    """Seats under this paying account, NOT counting the paying account's
    own login — matches how seat_limit reads in the pricing table (e.g.
    "2 Seats Included" on Pro means 1 extra seat beyond the owner login,
    matching MyTMTracker's own pricing copy)."""
    return conn.execute(
        "SELECT count(*) AS n FROM users WHERE parent_account_id = %s",
        (paying_account_id,),
    ).fetchone()["n"]
