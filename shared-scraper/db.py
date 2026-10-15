"""PostgreSQL storage for scraped trademark data (quickcompany.in + IP India portal)."""

import re
import threading
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import config

TRADEMARK_SCHEMA = """
CREATE TABLE IF NOT EXISTS trademarks (
    id                   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query                TEXT NOT NULL,
    name                 TEXT,
    profile_url          TEXT UNIQUE,
    application_id       TEXT,
    status               TEXT,
    sub_status           TEXT,
    date_of_application  TEXT,
    classes              TEXT,
    type                 TEXT,
    state                TEXT,
    country              TEXT,
    filing_mode          TEXT,
    ip_office            TEXT,
    used_since           TEXT,
    valid_upto           TEXT,
    description          TEXT,
    pr_details           TEXT,
    proprietor           TEXT,
    agent                TEXT,
    error                TEXT,
    scraped_at           TIMESTAMPTZ DEFAULT now()
);
"""

TM_FIELD_TO_COLUMN = {
    "Application ID": "application_id",
    "Status": "status",
    "Sub Status": "sub_status",
    "Date of Application": "date_of_application",
    "Classes": "classes",
    "Type": "type",
    "State": "state",
    "Country": "country",
    "Filing Mode": "filing_mode",
    "IP Office": "ip_office",
    "Used Since": "used_since",
    "Valid / Upto": "valid_upto",
    "Description": "description",
    "PR Details": "pr_details",
    "Proprietor": "proprietor",
    "Agent": "agent",
}


IPINDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS ipindia_results (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query       TEXT NOT NULL,
    search_type TEXT NOT NULL DEFAULT 'name',
    email_used  TEXT,
    data_json   JSONB,
    error       TEXT,
    scraped_at  TIMESTAMPTZ DEFAULT now()
);
"""

IPINDIA_ROW_SCHEMA = """
CREATE TABLE IF NOT EXISTS ipindia_rows (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    search_id  INTEGER NOT NULL REFERENCES ipindia_results(id) ON DELETE CASCADE,
    field_key  TEXT NOT NULL,
    field_val  TEXT,
    row_index  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ipindia_rows_search_id ON ipindia_rows(search_id);
"""

TMA_DOCUMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tma_documents (
    id                       INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    app_no                   TEXT NOT NULL,
    search_id                INTEGER REFERENCES ipindia_results(id) ON DELETE SET NULL,
    form_type                TEXT NOT NULL DEFAULT 'TM-A',
    source_pdf_filename      TEXT,
    source_pdf_path          TEXT,
    temp_ref_no              TEXT,
    fee                      TEXT,
    application_filed_as     TEXT,
    applicant_name           TEXT,
    applicant_partners       TEXT,
    applicant_address        TEXT,
    applicant_address_for_service TEXT,
    applicant_country        TEXT,
    applicant_jurisdiction   TEXT,
    applicant_mobile         TEXT,
    applicant_email          TEXT,
    applicant_nature         TEXT,
    applicant_legal_status   TEXT,
    agent_name               TEXT,
    agent_address            TEXT,
    agent_nature             TEXT,
    agent_registration_no    TEXT,
    mark_category            TEXT,
    mark_trade_mark          TEXT,
    mark_image_description   TEXT,
    language                 TEXT,
    class_number             TEXT,
    class_description        TEXT,
    use_statement            TEXT,
    other_information        TEXT,
    verification_date        TEXT,
    verification_signed_for  TEXT,
    receipt_no               TEXT,
    receipt_date              TEXT,
    receipt_amount           TEXT,
    form_no                  TEXT,
    agent_code               TEXT,
    proprietor_code          TEXT,
    application_date         TEXT,
    registry_office          TEXT,
    logo_path                TEXT,
    raw_json                 JSONB,
    extracted_at             TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tma_documents_app_no ON tma_documents(app_no);
CREATE INDEX IF NOT EXISTS idx_tma_documents_search_id ON tma_documents(search_id);
ALTER TABLE tma_documents ADD COLUMN IF NOT EXISTS logo_path TEXT;
-- Google Drive migration (see google_drive.py) — nullable, additive, zero
-- behavior change for any row that's never been uploaded. drive_file_id
-- is the source_pdf_path's PDF; logo_drive_file_id is the separate logo
-- PNG's own Drive file, since one tma_documents row can reference two
-- distinct physical files. local_deleted_at is set only by the standalone
-- cleanup script (scratch/backfill_drive_migration.py), never by the live
-- upload hook — the two are deliberately decoupled, see that script's
-- module docstring.
ALTER TABLE tma_documents ADD COLUMN IF NOT EXISTS drive_file_id TEXT;
ALTER TABLE tma_documents ADD COLUMN IF NOT EXISTS logo_drive_file_id TEXT;
ALTER TABLE tma_documents ADD COLUMN IF NOT EXISTS drive_uploaded_at TIMESTAMPTZ;
ALTER TABLE tma_documents ADD COLUMN IF NOT EXISTS local_deleted_at TIMESTAMPTZ;
ALTER TABLE tma_documents ADD COLUMN IF NOT EXISTS fleet_canonical BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tma_documents ADD COLUMN IF NOT EXISTS fleet_sha256 TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS tma_fleet_content ON tma_documents(app_no,fleet_sha256) WHERE fleet_sha256 IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS tma_fleet_canonical ON tma_documents(app_no) WHERE fleet_canonical;
"""

IPINDIA_REMINDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ipindia_reminders (
    id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    app_no         TEXT NOT NULL,
    search_id      INTEGER REFERENCES ipindia_results(id) ON DELETE SET NULL,
    s_no           TEXT,
    corres_no      TEXT,
    corres_date    TEXT,
    subject        TEXT,
    despatch_no    TEXT,
    despatch_date  TEXT,
    corres_num     TEXT,
    document_path  TEXT,
    fetched_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ipindia_reminders_app_no ON ipindia_reminders(app_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipindia_reminders_app_corres
    ON ipindia_reminders(app_no, corres_no);
-- Google Drive migration — see tma_documents' identical columns above for
-- the rationale (nullable, additive, local_deleted_at set only by the
-- standalone cleanup script, never the live upload hook).
ALTER TABLE ipindia_reminders ADD COLUMN IF NOT EXISTS drive_file_id TEXT;
ALTER TABLE ipindia_reminders ADD COLUMN IF NOT EXISTS drive_uploaded_at TIMESTAMPTZ;
ALTER TABLE ipindia_reminders ADD COLUMN IF NOT EXISTS local_deleted_at TIMESTAMPTZ;
"""

IPINDIA_CORRESPONDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ipindia_correspondence (
    id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    app_no         TEXT NOT NULL,
    search_id      INTEGER REFERENCES ipindia_results(id) ON DELETE SET NULL,
    s_no           TEXT,
    corres_no      TEXT,
    corres_date    TEXT,
    subject        TEXT,
    despatch_no    TEXT,
    despatch_date  TEXT,
    view_url       TEXT,
    document_path  TEXT,
    fetched_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ipindia_correspondence_app_no ON ipindia_correspondence(app_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipindia_correspondence_app_corres
    ON ipindia_correspondence(app_no, corres_no);
-- Google Drive migration — see tma_documents' identical columns above.
ALTER TABLE ipindia_correspondence ADD COLUMN IF NOT EXISTS drive_file_id TEXT;
ALTER TABLE ipindia_correspondence ADD COLUMN IF NOT EXISTS drive_uploaded_at TIMESTAMPTZ;
ALTER TABLE ipindia_correspondence ADD COLUMN IF NOT EXISTS local_deleted_at TIMESTAMPTZ;
"""

DRIVE_LOGO_FILES_SCHEMA = """
-- Google Drive migration — tracks logo image files that have NO
-- tma_documents row at all (e.g. trademark-image.png, saved by
-- IPIndiaSession.get_trademark_image, a separate code path from
-- process_pdf's document/logo extraction). tma_documents.logo_path /
-- .logo_drive_file_id remain authoritative for a PDF-extracted logo;
-- this table exists only for the glob-only case _serve_logo_for_app_no
-- already falls back to today (trademark-image.png / *logo*.png|jpg with
-- no DB row). One row per (app_no, local_filename) actually uploaded.
CREATE TABLE IF NOT EXISTS drive_logo_files (
    id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    app_no            TEXT NOT NULL,
    local_filename    TEXT NOT NULL,
    drive_file_id     TEXT NOT NULL,
    drive_uploaded_at TIMESTAMPTZ DEFAULT now(),
    local_deleted_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_drive_logo_files_app_no_filename
    ON drive_logo_files(app_no, local_filename);
"""

IPINDIA_PAGE_FETCHES_SCHEMA = """
-- Raw HTML for the two portal panels that have no field-extraction logic
-- yet: PR Details (RegisteredTM/GetPRDetails) and the "e-main page" /
-- ViewAppDetails modal (RegisteredTM/Details). Stored as-is, unparsed —
-- per this project's fetch/format split, the scraper's job here is just to
-- get the data down reliably; turning raw_html into structured fields is a
-- separate concern that can be built later without re-fetching anything.
CREATE TABLE IF NOT EXISTS ipindia_page_fetches (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    app_no      TEXT NOT NULL,
    search_id   INTEGER REFERENCES ipindia_results(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('pr_details', 'app_details')),
    raw_html    TEXT,
    fetched_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ipindia_page_fetches_app_no ON ipindia_page_fetches(app_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipindia_page_fetches_app_kind
    ON ipindia_page_fetches(app_no, kind);
"""

IPINDIA_TRADEMARK_IMAGES_SCHEMA = """
-- The trademark logo/device image fetched directly from the portal's own
-- /estatus/trademark/image endpoint (see IPIndiaSession.get_trademark_image),
-- kept independent of tma_documents on purpose: that table stores one row
-- PER PARSED DOCUMENT, and tm_manager's get_trademark_data always reads
-- just the newest such row — an image-only row there would become "latest"
-- and blank out a real PDF's already-saved Application Date/Class/etc.
-- fields. This table is the image's own home, one row per app_no,
-- independent of whether any document ever parses successfully — the
-- image is available even for hits with zero uploaded documents.
CREATE TABLE IF NOT EXISTS ipindia_trademark_images (
    app_no      TEXT PRIMARY KEY,
    image_path  TEXT NOT NULL,
    fetched_at  TIMESTAMPTZ DEFAULT now()
);
"""

IPINDIA_SERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS ipindia_series_progress (
    series               TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'queued',
    queue_position       INTEGER,
    range_start          INTEGER NOT NULL,
    range_end            INTEGER NOT NULL,
    last_checked_number  INTEGER,
    hits_count           INTEGER NOT NULL DEFAULT 0,
    misses_count         INTEGER NOT NULL DEFAULT 0,
    errors_count         INTEGER NOT NULL DEFAULT 0,
    claimed_by_job_id    TEXT,
    email_used           TEXT,
    started_at           TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ipindia_series_progress_status ON ipindia_series_progress(status);
CREATE INDEX IF NOT EXISTS idx_ipindia_series_progress_queue_position ON ipindia_series_progress(queue_position);
"""

OTP_EMAIL_POOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS otp_email_pool (
    id                 INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email_address      TEXT UNIQUE NOT NULL,
    app_password       TEXT NOT NULL,
    imap_server        TEXT NOT NULL DEFAULT 'imap.mail.yahoo.com',
    imap_port          INTEGER NOT NULL DEFAULT 993,
    enabled            BOOLEAN NOT NULL DEFAULT true,
    claimed_by_job_id  TEXT,
    claimed_at         TIMESTAMPTZ,
    last_used_at       TIMESTAMPTZ,
    last_test_ok       BOOLEAN,
    last_test_message  TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_otp_email_pool_claimed ON otp_email_pool(claimed_by_job_id);
ALTER TABLE otp_email_pool ADD COLUMN IF NOT EXISTS reserved_for TEXT;
-- assigned_machine_id: Phase 0 of the Contabo-coordinator + local-worker-
-- fleet migration (see the plan doc). Points at tm_manager's
-- fleet_machines.id, but deliberately has NO foreign key — this table and
-- fleet_machines are migrated by two independent processes (this scraper
-- app and tm_manager) against the same Postgres database, each running its
-- own _ensure_schema on its own startup with no guaranteed ordering
-- between them; a real FK here would make this ALTER fail outright if the
-- scraper happens to start before tm_manager has ever created
-- fleet_machines. tm_manager is the only writer of this column (via its
-- fleet dashboard), so referential integrity is enforced at that
-- application layer instead. NULL (every existing row's default) means
-- "not assigned to any worker machine — stays in Contabo's own shared/cold
-- pool, or reserved for warm_pool/wordmark_pool exactly as today" — zero
-- behavior change until something actually reads this column.
ALTER TABLE otp_email_pool ADD COLUMN IF NOT EXISTS assigned_machine_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_otp_email_pool_assigned_machine ON otp_email_pool(assigned_machine_id);
"""


WORK_ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    app_no       TEXT UNIQUE NOT NULL,
    work_status  TEXT NOT NULL DEFAULT 'Talking',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_work_items_closed ON work_items(closed_at);
"""

WORK_NOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_notes (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    work_item_id  INTEGER NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    note          TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_work_notes_work_item_id ON work_notes(work_item_id);
"""

WORK_STATUSES = ["Talking", "Follow-up", "Documents Pending", "Filed", "Closed"]

SCRAPE_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS scrape_jobs (
    id            UUID PRIMARY KEY,
    app_no        TEXT NOT NULL,
    search_type   TEXT NOT NULL DEFAULT 'appno',
    status        TEXT NOT NULL DEFAULT 'running',   -- running | done | error
    step          TEXT,
    search_id     INTEGER,
    result_count  INTEGER,
    error         TEXT,
    used_warm     BOOLEAN DEFAULT false,
    requested_by  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_scrape_jobs_created ON scrape_jobs(created_at DESC);
"""


WORDMARK_SEARCHES_SCHEMA = """
-- One row per Word Mark + Class search run (tmrpublicsearch portal, see
-- app.py's /api/scrape-wordmark-class). Kept separately from ipindia_results
-- since a wordmark search can return thousands of rows (its own child
-- table, wordmark_search_results, below) rather than one data_json blob -
-- that would make a single huge JSONB value for a 5000+ result search.
CREATE TABLE IF NOT EXISTS wordmark_searches (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    search_text   TEXT NOT NULL,
    search_option TEXT NOT NULL,       -- start_with | contains | match_with
    class_code    INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',  -- running | done | error
    result_count  INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    requested_by  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_wordmark_searches_created ON wordmark_searches(created_at DESC);
"""

WORDMARK_SEARCH_RESULTS_SCHEMA = """
-- Child rows of one wordmark_searches run - inserted incrementally AS they
-- are parsed (not all at once at the end) so a client polling the parent
-- search's progress can render results live instead of waiting for a
-- 5000+ row search to fully finish. logo_data_uri is often several KB of
-- base64 per row (see _parse_wordmark_results) - kept here rather than a
-- separate table since it's small enough per-row and this table is already
-- keyed for bulk per-search reads (history view, Excel export).
CREATE TABLE IF NOT EXISTS wordmark_search_results (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    wordmark_search_id INTEGER NOT NULL REFERENCES wordmark_searches(id) ON DELETE CASCADE,
    app_no           TEXT,
    class_number     TEXT,
    application_date TEXT,
    trade_mark       TEXT,
    proprietor_name  TEXT,
    use_statement    TEXT,
    valid_upto       TEXT,
    status           TEXT,
    logo_data_uri    TEXT,
    class_description TEXT,
    row_order        INTEGER NOT NULL,   -- preserves the portal's own result order
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wsr_search ON wordmark_search_results(wordmark_search_id, row_order);
"""

WORDMARK_EXCEL_JOBS_SCHEMA = """
-- Tracks one background "build an .xlsx for this search" job so the
-- frontend can poll it (same pattern as scrape_jobs) instead of holding an
-- HTTP request open while a 5000+ row workbook is built.
CREATE TABLE IF NOT EXISTS wordmark_excel_jobs (
    id                  UUID PRIMARY KEY,
    wordmark_search_id  INTEGER NOT NULL REFERENCES wordmark_searches(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'running',  -- running | done | error
    error               TEXT,
    file_path           TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);
"""


_schema_ready = False
_schema_lock = threading.Lock()

# ── Connection pool ─────────────────────────────────────────────────────────
# Every caller does `conn = db.get_connection(); try: ... finally: conn.close()`.
# We keep that exact shape but hand back a pooled connection wrapped in a thin
# proxy whose .close() returns it to the pool instead of tearing down a TCP+auth
# every request. Zero call-site changes.

_POOL = None
_pool_lock = threading.Lock()


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn.execute(TRADEMARK_SCHEMA)
        conn.execute(IPINDIA_SCHEMA)
        conn.execute(IPINDIA_ROW_SCHEMA)
        conn.execute(TMA_DOCUMENTS_SCHEMA)
        conn.execute(IPINDIA_REMINDERS_SCHEMA)
        conn.execute(IPINDIA_CORRESPONDENCE_SCHEMA)
        conn.execute(DRIVE_LOGO_FILES_SCHEMA)
        conn.execute(IPINDIA_PAGE_FETCHES_SCHEMA)
        conn.execute(IPINDIA_TRADEMARK_IMAGES_SCHEMA)
        conn.execute(IPINDIA_SERIES_SCHEMA)
        conn.execute(OTP_EMAIL_POOL_SCHEMA)
        conn.execute(WORK_ITEMS_SCHEMA)
        conn.execute(WORK_NOTES_SCHEMA)
        conn.execute(SCRAPE_JOBS_SCHEMA)
        conn.execute(WORDMARK_SEARCHES_SCHEMA)
        conn.execute(WORDMARK_SEARCH_RESULTS_SCHEMA)
        conn.execute(WORDMARK_EXCEL_JOBS_SCHEMA)
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
            # Client-cert auth (see pg_hba.conf's "cert" rule on Contabo) —
            # authenticates this machine by identity instead of by IP, so a
            # VPN or ISP change never needs a new firewall/pg_hba rule.
            # sslmode=require (not verify-full): this only proves who WE
            # are to the server, it doesn't need to verify the server's own
            # (still-default self-signed) certificate.
            conninfo += f" sslmode=require sslcert={config.PG_SSLCERT} sslkey={config.PG_SSLKEY}"
        elif config.PG_PASSWORD:
            conninfo += f" password={config.PG_PASSWORD}"
        pool = ConnectionPool(
            conninfo,
            min_size=config.DB_POOL_MIN,
            max_size=config.DB_POOL_MAX,
            kwargs={"row_factory": dict_row},
            open=True,
            name="datascraper",
        )
        with pool.connection() as c:
            _ensure_schema(c)
        _POOL = pool
        return _POOL


class _PooledConnection:
    """Forwards everything to a real pooled connection; .close() returns it to
    the pool. Also usable as a context manager (commits/rolls back like psycopg,
    then returns to pool)."""

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
                # leave the session clean for the next borrower
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


def save_trademark_result(conn: psycopg.Connection, result: dict) -> None:
    row = {
        "query": result.get("query", ""),
        "name": result.get("name", ""),
        "profile_url": result.get("profile_url"),
        "error": result.get("error"),
    }
    for label, column in TM_FIELD_TO_COLUMN.items():
        row[column] = result.get(label, "")

    columns = ["query", "name", "profile_url", *TM_FIELD_TO_COLUMN.values(), "error"]
    col_list = ", ".join(columns)
    placeholders = ", ".join(f"%({c})s" for c in columns)

    if row["profile_url"]:
        update_clause = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "profile_url")
        conn.execute(
            f"""
            INSERT INTO trademarks ({col_list}, scraped_at)
            VALUES ({placeholders}, now())
            ON CONFLICT(profile_url) DO UPDATE SET
                {update_clause}, scraped_at=now()
            """,
            row,
        )
    else:
        conn.execute(
            f"""
            INSERT INTO trademarks ({col_list}, scraped_at)
            VALUES ({placeholders}, now())
            """,
            row,
        )
    conn.commit()


def list_trademark_searches(conn: psycopg.Connection) -> list[dict]:
    cursor = conn.execute(
        """
        SELECT query, COUNT(*) AS trademark_count, MAX(scraped_at) AS last_scraped_at
        FROM trademarks
        GROUP BY query
        ORDER BY last_scraped_at DESC
        """
    )
    return cursor.fetchall()


def trademarks_for_query(conn: psycopg.Connection, query: str) -> list[dict]:
    cursor = conn.execute(
        "SELECT * FROM trademarks WHERE query = %s ORDER BY LOWER(name)",
        (query,),
    )
    return cursor.fetchall()


def get_trademark(conn: psycopg.Connection, trademark_id: int) -> dict | None:
    cursor = conn.execute("SELECT * FROM trademarks WHERE id = %s", (trademark_id,))
    return cursor.fetchone()


def search_ipindia_results(
    conn: psycopg.Connection,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    agent_name: str | None = None,
) -> list[dict]:
    """
    Combined search across IP India results: any mix of date range, status,
    and agent name, all AND-ed together in SQL (all are cheap/indexable —
    date on scraped_at, status inside data_json, agent on tma_documents).
    Applicant type and contact visibility are NOT filtered here since
    they're computed in Python from free-text names (see
    ipindia_grouping.classify_applicant_type /
    classify_contact_visibility) — callers narrow the returned rows further
    for those two dimensions after this fetch.

    Returns one row per ipindia_results entry (newest first), each carrying
    enough tma_documents fields (applicant name, agent name, mobile, email)
    for every filter dimension to be applied/displayed downstream.
    """
    where = ["1=1"]
    params: dict = {}

    if date_from:
        where.append("r.scraped_at::date >= %(date_from)s::date")
        params["date_from"] = date_from
    if date_to:
        where.append("r.scraped_at::date <= %(date_to)s::date")
        params["date_to"] = date_to
    if status:
        where.append("r.data_json->0->>'Status' = %(status)s")
        params["status"] = status
    if agent_name:
        where.append("t.agent_name = %(agent_name)s")
        params["agent_name"] = agent_name

    query = f"""
        SELECT DISTINCT ON (r.id)
               r.id, r.query, r.search_type, r.email_used,
               jsonb_array_length(r.data_json) AS result_count,
               r.error, r.scraped_at,
               r.data_json->0->>'Status' AS status,
               r.data_json->0->>'Proprietor Name' AS proprietor_name,
               t.applicant_name, t.agent_name,
               t.applicant_mobile, t.applicant_email, t.logo_path
        FROM ipindia_results r
        LEFT JOIN tma_documents t ON t.app_no = r.query
        WHERE {' AND '.join(where)}
        ORDER BY r.id, t.extracted_at DESC NULLS LAST
    """
    cursor = conn.execute(query, params)
    rows = cursor.fetchall()
    rows.sort(key=lambda r: r["scraped_at"], reverse=True)
    return rows


def ipindia_already_scraped_app_nos(conn: psycopg.Connection, app_nos: list[str]) -> set[str]:
    """Given a list of candidate application numbers, return the subset that
    already has a saved IP India appno search — used to skip re-scraping
    when expanding a cluster via QuickCompany."""
    if not app_nos:
        return set()
    cursor = conn.execute(
        """
        SELECT DISTINCT query FROM ipindia_results
        WHERE search_type IN ('appno', 'appno_discover', 'appno_batch') AND query = ANY(%s)
        """,
        (app_nos,),
    )
    return {row["query"] for row in cursor.fetchall()}


def ipindia_scraped_app_nos_in_range(conn: psycopg.Connection, range_start: int, range_end: int) -> set[int]:
    """
    Every application number (as int) within [range_start, range_end] that
    already has a saved hit — used by the series tracker's sequential
    scanner to skip numbers it already has data for (found earlier by
    other scraping — random-mode Auto Discover, manual searches — ahead of
    this series' own sequential checkpoint) instead of wastefully
    re-searching the live portal for something already known.
    """
    cursor = conn.execute(
        """
        SELECT DISTINCT query FROM ipindia_results
        WHERE search_type IN ('appno', 'appno_discover', 'appno_batch')
          AND query ~ '^[0-9]+$'
          AND query::bigint BETWEEN %s AND %s
        """,
        (range_start, range_end),
    )
    return {int(row["query"]) for row in cursor.fetchall()}


# ── IP India e-Status results ─────────────────────────────────────────────────

def save_ipindia_search(
    conn: psycopg.Connection,
    query: str,
    search_type: str,
    email_used: str,
    results: list[dict],
    error: str = "",
) -> int:
    """
    Persist a complete IP India search (header + rows).
    Returns the new search ID.
    """
    cur = conn.execute(
        """
        INSERT INTO ipindia_results (query, search_type, email_used, data_json, error, scraped_at)
        VALUES (%s, %s, %s, %s, %s, now())
        RETURNING id
        """,
        (query, search_type, email_used, Jsonb(results), error),
    )
    search_id = cur.fetchone()["id"]

    field_rows = [
        (search_id, str(key), str(val) if val is not None else "", row_idx)
        for row_idx, result in enumerate(results)
        for key, val in result.items()
    ]
    if field_rows:
        # One round-trip for all fields instead of one INSERT per field —
        # with a remote (Contabo) Postgres, per-field round trips were the
        # actual bottleneck behind "scraping is fast, saving is slow": a
        # single hit with ~25 fields meant ~25 sequential network round
        # trips just to persist it.
        with conn.cursor() as batch_cur:
            batch_cur.executemany(
                """
                INSERT INTO ipindia_rows (search_id, field_key, field_val, row_index)
                VALUES (%s, %s, %s, %s)
                """,
                field_rows,
            )
    conn.commit()
    return search_id


def get_previous_ipindia_status(conn: psycopg.Connection, query: str, exclude_id: int) -> str | None:
    """Status of the most recent ipindia_results row for this app_no,
    excluding exclude_id (the row just written by save_ipindia_search for
    the current fetch) — so this always looks at the fetch immediately
    BEFORE the current one, never compares the new row against itself.
    Returns None if there's no earlier row (first-ever fetch for this
    app_no) — callers must treat None as "always do the full fetch."
    search_type is intentionally not filtered — a prior 'appno',
    'appno_discover', or 'appno_batch' row is equally valid history for
    "what was the status last time we checked."
    """
    cursor = conn.execute(
        """
        SELECT data_json->0->>'Status' AS status
        FROM ipindia_results
        WHERE query = %(query)s AND id != %(exclude_id)s
        ORDER BY scraped_at DESC, id DESC
        LIMIT 1
        """,
        {"query": query, "exclude_id": exclude_id},
    )
    row = cursor.fetchone()
    return row["status"] if row else None


def create_wordmark_search(
    conn: psycopg.Connection, search_text: str, search_option: str, class_code: int,
    requested_by: str | None = None,
) -> int:
    """Start a new Word Mark + Class search record — row-count and status
    are filled in as results stream in (append_wordmark_results) and when
    the search finishes (finish_wordmark_search)."""
    cur = conn.execute(
        """
        INSERT INTO wordmark_searches (search_text, search_option, class_code, requested_by)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (search_text, search_option, class_code, requested_by),
    )
    search_id = cur.fetchone()["id"]
    conn.commit()
    return search_id


def append_wordmark_results(conn: psycopg.Connection, search_id: int, results: list[dict], start_order: int) -> None:
    """Insert a batch of parsed result rows for an in-progress search and
    bump wordmark_searches.result_count to match — called repeatedly as the
    scraper parses more of the portal's result table (see
    ipindia_scraper._parse_wordmark_results's chunked caller), so a client
    polling the search's progress sees the count/rows grow live instead of
    only appearing once the whole search is done."""
    if not results:
        return
    for i, r in enumerate(results):
        conn.execute(
            """
            INSERT INTO wordmark_search_results
                (wordmark_search_id, app_no, class_number, application_date, trade_mark,
                 proprietor_name, use_statement, valid_upto, status, logo_data_uri,
                 class_description, row_order)
            VALUES (%(sid)s, %(app_no)s, %(class_number)s, %(application_date)s, %(trade_mark)s,
                    %(proprietor_name)s, %(use_statement)s, %(valid_upto)s, %(status)s, %(logo_data_uri)s,
                    %(class_description)s, %(row_order)s)
            """,
            {
                "sid": search_id, "app_no": r.get("app_no"), "class_number": r.get("class_number"),
                "application_date": r.get("application_date"), "trade_mark": r.get("trade_mark"),
                "proprietor_name": r.get("proprietor_name"), "use_statement": r.get("use_statement"),
                "valid_upto": r.get("valid_upto"), "status": r.get("status"),
                "logo_data_uri": r.get("logo_data_uri"), "class_description": r.get("class_description"),
                "row_order": start_order + i,
            },
        )
    conn.execute(
        "UPDATE wordmark_searches SET result_count = result_count + %s WHERE id = %s",
        (len(results), search_id),
    )
    conn.commit()


def finish_wordmark_search(conn: psycopg.Connection, search_id: int, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE wordmark_searches SET status = %s, error = %s, finished_at = now() WHERE id = %s",
        (status, error, search_id),
    )
    conn.commit()


def delete_wordmark_search(conn: psycopg.Connection, search_id: int) -> None:
    """Remove a search and its rows entirely — used for zero-result
    searches (nothing worth keeping in History) right after they finish.
    wordmark_search_results and wordmark_excel_jobs both cascade on this
    delete (ON DELETE CASCADE in their schema)."""
    conn.execute("DELETE FROM wordmark_searches WHERE id = %s", (search_id,))
    conn.commit()


def list_wordmark_searches(conn: psycopg.Connection, limit: int = 30) -> list[dict]:
    cursor = conn.execute(
        """
        SELECT id, search_text, search_option, class_code, status, result_count, error,
               requested_by, created_at, finished_at
        FROM wordmark_searches
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return cursor.fetchall()


def get_wordmark_search(conn: psycopg.Connection, search_id: int) -> dict | None:
    cursor = conn.execute(
        """
        SELECT id, search_text, search_option, class_code, status, result_count, error,
               requested_by, created_at, finished_at
        FROM wordmark_searches WHERE id = %s
        """,
        (search_id,),
    )
    return cursor.fetchone()


def list_wordmark_search_results(
    conn: psycopg.Connection, search_id: int, after_row_order: int = -1, limit: int | None = None,
) -> list[dict]:
    """Rows for one search, in portal order. after_row_order lets a poller
    fetch only what's new since its last check (incremental streaming);
    limit=None (Excel export) fetches everything."""
    query = (
        "SELECT app_no, class_number, application_date, trade_mark, proprietor_name, "
        "use_statement, valid_upto, status, logo_data_uri, class_description, row_order "
        "FROM wordmark_search_results WHERE wordmark_search_id = %(sid)s AND row_order > %(after)s "
        "ORDER BY row_order"
    )
    params = {"sid": search_id, "after": after_row_order}
    if limit is not None:
        query += " LIMIT %(limit)s"
        params["limit"] = limit
    cursor = conn.execute(query, params)
    return cursor.fetchall()


def create_wordmark_excel_job(conn: psycopg.Connection, job_id: str, search_id: int) -> None:
    conn.execute(
        "INSERT INTO wordmark_excel_jobs (id, wordmark_search_id) VALUES (%s, %s)",
        (job_id, search_id),
    )
    conn.commit()


def finish_wordmark_excel_job(conn: psycopg.Connection, job_id: str, status: str, file_path: str | None = None, error: str | None = None) -> None:
    conn.execute(
        "UPDATE wordmark_excel_jobs SET status = %s, file_path = %s, error = %s, finished_at = now() WHERE id = %s",
        (status, file_path, error, job_id),
    )
    conn.commit()


def get_wordmark_excel_job(conn: psycopg.Connection, job_id: str) -> dict | None:
    cursor = conn.execute("SELECT * FROM wordmark_excel_jobs WHERE id = %s", (job_id,))
    return cursor.fetchone()


def list_ipindia_searches(conn: psycopg.Connection) -> list[dict]:
    cursor = conn.execute(
        """
        SELECT id, query, search_type, email_used,
               jsonb_array_length(data_json) AS result_count,
               error, scraped_at
        FROM ipindia_results
        ORDER BY scraped_at DESC
        """
    )
    return cursor.fetchall()


def count_ipindia_hits_in_range(conn: psycopg.Connection, range_start: int, range_end: int) -> int:
    """
    Count of UNIQUE application numbers with a saved hit within
    [range_start, range_end] — the real "how much data do we actually
    have" figure for a series, independent of any single Auto Discover
    job's own progress counters (ipindia_series_progress.hits_count only
    reflects what one tracked job has personally found since its own
    checkpoint, which understates reality once other scraping — earlier
    random-mode runs, manual searches — has already covered numbers ahead
    of that checkpoint).
    """
    cursor = conn.execute(
        """
        SELECT COUNT(DISTINCT query) AS n
        FROM ipindia_results
        WHERE search_type IN ('appno', 'appno_discover', 'appno_batch')
          AND query ~ '^[0-9]+$'
          AND query::bigint BETWEEN %s AND %s
        """,
        (range_start, range_end),
    )
    return cursor.fetchone()["n"]


def list_ipindia_hits_in_range(conn: psycopg.Connection, range_start: int, range_end: int) -> list[dict]:
    """
    One row per UNIQUE application number (not per scrape) whose query
    falls within [range_start, range_end] — feeds a series' detail page so
    its already-found hits are browsable (including logo thumbnails, same
    as the search history page) without a separate per-number log table.
    An app number scraped more than once (e.g. re-discovered by two Auto
    Discover runs) collapses to its MOST RECENT scrape here; fetch_count
    tells the caller how many scrapes exist so the UI can link to
    "see all N fetches" (see list_ipindia_fetches_for_app_no). query is
    stored as TEXT so the numeric comparison casts it; non-numeric queries
    (name searches) never match a numeric range and are naturally excluded.
    """
    cursor = conn.execute(
        """
        SELECT DISTINCT ON (r.query)
               r.id, r.query, r.search_type, r.email_used,
               r.data_json->0->>'Trade Mark' AS trade_mark,
               r.data_json->0->>'Status' AS status,
               r.error, r.scraped_at, t.logo_path,
               fc.fetch_count
        FROM ipindia_results r
        LEFT JOIN tma_documents t ON t.app_no = r.query
        JOIN (
            SELECT query, COUNT(*) AS fetch_count
            FROM ipindia_results
            WHERE search_type IN ('appno', 'appno_discover', 'appno_batch')
              AND query ~ '^[0-9]+$'
              AND query::bigint BETWEEN %s AND %s
            GROUP BY query
        ) fc ON fc.query = r.query
        WHERE r.search_type IN ('appno', 'appno_discover', 'appno_batch')
          AND r.query ~ '^[0-9]+$'
          AND r.query::bigint BETWEEN %s AND %s
        ORDER BY r.query, r.scraped_at DESC, t.extracted_at DESC NULLS LAST
        """,
        (range_start, range_end, range_start, range_end),
    )
    rows = cursor.fetchall()
    rows.sort(key=lambda r: int(r["query"]))
    return rows


def list_ipindia_fetches_for_app_no(conn: psycopg.Connection, app_no: str) -> list[dict]:
    """Every scrape ever saved for one specific application number (appno
    or appno_discover), newest first — used by the "N fetches" link on a
    series detail page to show the full re-scrape history for a number."""
    cursor = conn.execute(
        """
        SELECT r.id, r.query, r.search_type, r.email_used,
               r.data_json->0->>'Trade Mark' AS trade_mark,
               r.data_json->0->>'Status' AS status,
               r.error, r.scraped_at
        FROM ipindia_results r
        WHERE r.search_type IN ('appno', 'appno_discover', 'appno_batch') AND r.query = %s
        ORDER BY r.scraped_at DESC
        """,
        (app_no,),
    )
    return cursor.fetchall()


def list_ipindia_search_dates(conn: psycopg.Connection) -> list[dict]:
    """One row per calendar date (local session timezone) that has at least
    one saved search, newest first, with a count of searches that day."""
    cursor = conn.execute(
        """
        SELECT scraped_at::date AS search_date,
               COUNT(*) AS search_count
        FROM ipindia_results
        GROUP BY scraped_at::date
        ORDER BY search_date DESC
        """
    )
    return cursor.fetchall()


def list_ipindia_searches_for_date(conn: psycopg.Connection, date_str: str) -> list[dict]:
    """All ipindia_results rows scraped on one calendar date (local
    timezone). Grouping into ranges happens separately in Python
    (see ipindia_grouping.group_searches_for_date)."""
    cursor = conn.execute(
        """
        SELECT id, query, search_type, email_used,
               jsonb_array_length(data_json) AS result_count,
               error, scraped_at
        FROM ipindia_results
        WHERE scraped_at::date = %s::date
        ORDER BY scraped_at ASC
        """,
        (date_str,),
    )
    return cursor.fetchall()


def list_ipindia_status_counts(conn: psycopg.Connection) -> list[dict]:
    """
    One row per distinct trademark Status value actually present in saved
    results (from the first result row's "Status" field), with a count.
    Blank/missing statuses are excluded.
    """
    cursor = conn.execute(
        """
        SELECT data_json->0->>'Status' AS status, COUNT(*) AS result_count
        FROM ipindia_results
        WHERE data_json IS NOT NULL
          AND jsonb_array_length(data_json) > 0
          AND data_json->0->>'Status' IS NOT NULL
          AND data_json->0->>'Status' != ''
        GROUP BY data_json->0->>'Status'
        ORDER BY result_count DESC
        """
    )
    return cursor.fetchall()


def list_ipindia_searches_by_status(
    conn: psycopg.Connection, status: str, limit: int, offset: int
) -> tuple[list[dict], int]:
    """
    Searches whose first result's Status matches exactly, newest first.
    Returns (page_of_rows, total_matching_count).
    """
    total = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM ipindia_results
        WHERE data_json IS NOT NULL
          AND jsonb_array_length(data_json) > 0
          AND data_json->0->>'Status' = %s
        """,
        (status,),
    ).fetchone()["n"]

    cursor = conn.execute(
        """
        SELECT id, query, search_type, email_used,
               jsonb_array_length(data_json) AS result_count,
               error, scraped_at
        FROM ipindia_results
        WHERE data_json IS NOT NULL
          AND jsonb_array_length(data_json) > 0
          AND data_json->0->>'Status' = %s
        ORDER BY scraped_at DESC
        LIMIT %s OFFSET %s
        """,
        (status, limit, offset),
    )
    return cursor.fetchall(), total


def list_ipindia_searches_with_proprietor(conn: psycopg.Connection) -> list[dict]:
    """
    Every search that has a first-result "Proprietor Name", newest first.
    Applicant-type classification (Private Limited, LLP, Proprietorship,
    etc.) happens in Python — see ipindia_grouping.classify_applicant_type —
    since it's pattern-matched from the name text, not a clean SQL-filterable
    column.
    """
    cursor = conn.execute(
        """
        SELECT id, query, search_type, email_used,
               jsonb_array_length(data_json) AS result_count,
               error, scraped_at,
               data_json->0->>'Proprietor Name' AS proprietor_name
        FROM ipindia_results
        WHERE data_json IS NOT NULL
          AND jsonb_array_length(data_json) > 0
          AND data_json->0->>'Proprietor Name' IS NOT NULL
          AND data_json->0->>'Proprietor Name' != ''
        ORDER BY scraped_at DESC
        """
    )
    return cursor.fetchall()


def list_tma_documents_with_contact_info(conn: psycopg.Connection) -> list[dict]:
    """
    Every tma_documents row that has an applicant mobile and/or email,
    joined back to its most recent matching ipindia_results row (by app_no)
    for display purposes. Contact-visibility classification (masked by the
    portal vs. shown in full) happens in Python — see
    ipindia_grouping.classify_contact_visibility.
    """
    cursor = conn.execute(
        """
        SELECT DISTINCT ON (t.app_no)
               t.app_no, t.applicant_mobile, t.applicant_email,
               t.applicant_name, t.form_type,
               r.id AS search_id, r.scraped_at
        FROM tma_documents t
        LEFT JOIN ipindia_results r ON r.query = t.app_no
        WHERE t.applicant_mobile IS NOT NULL OR t.applicant_email IS NOT NULL
        ORDER BY t.app_no, r.scraped_at DESC NULLS LAST
        """
    )
    return cursor.fetchall()


def list_tma_agent_counts(conn: psycopg.Connection) -> list[dict]:
    """
    One row per distinct Agent name that filed at least one application,
    with a count of how many. Agent names are clean structured data (unlike
    Legal Status), so this groups directly in SQL — no Python classification
    needed, unlike applicant type/contact visibility.
    """
    cursor = conn.execute(
        """
        SELECT agent_name, COUNT(*) AS result_count
        FROM tma_documents
        WHERE agent_name IS NOT NULL AND agent_name != ''
        GROUP BY agent_name
        ORDER BY result_count DESC, agent_name ASC
        """
    )
    return cursor.fetchall()


def list_tma_documents_by_agent(
    conn: psycopg.Connection, agent_name: str, limit: int, offset: int
) -> tuple[list[dict], int]:
    """
    Applications filed by one agent (exact name match), newest first, joined
    back to the matching ipindia_results row (by app_no) for display.
    Returns (page_of_rows, total_matching_count).
    """
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM tma_documents WHERE agent_name = %s",
        (agent_name,),
    ).fetchone()["n"]

    cursor = conn.execute(
        """
        SELECT DISTINCT ON (t.app_no)
               t.app_no, t.applicant_name, t.applicant_mobile, t.applicant_email,
               t.mark_trade_mark, t.form_type,
               t.mark_category, t.class_number, t.class_description,
               r.id AS search_id, r.scraped_at
        FROM tma_documents t
        LEFT JOIN ipindia_results r ON r.query = t.app_no
        WHERE t.agent_name = %s
        ORDER BY t.app_no, r.scraped_at DESC NULLS LAST
        LIMIT %s OFFSET %s
        """,
        (agent_name, limit, offset),
    )
    return cursor.fetchall(), total


def get_tma_agent_profile(conn: psycopg.Connection, agent_name: str) -> dict | None:
    """
    The agent/advocate's own contact details (address, nature, registration
    no, agent code) as extracted from their most recently filed application.
    tma_documents has no dedicated agent-phone/email columns — those fields
    are only captured for applicants, not agents — so this returns whatever
    the source PDFs actually contain.
    """
    cursor = conn.execute(
        """
        SELECT agent_name, agent_address, agent_nature, agent_registration_no, agent_code
        FROM tma_documents
        WHERE agent_name = %s
        ORDER BY extracted_at DESC
        LIMIT 1
        """,
        (agent_name,),
    )
    return cursor.fetchone()


def get_tma_agent_category_breakdown(conn: psycopg.Connection, agent_name: str) -> list[dict]:
    """Count of applications filed by this agent, grouped by trademark category (WORD/DEVICE/etc)."""
    cursor = conn.execute(
        """
        SELECT COALESCE(NULLIF(mark_category, ''), 'Unspecified') AS category, COUNT(*) AS n
        FROM tma_documents
        WHERE agent_name = %s
        GROUP BY category
        ORDER BY n DESC, category ASC
        """,
        (agent_name,),
    )
    return cursor.fetchall()


def get_ipindia_search(conn: psycopg.Connection, search_id: int) -> dict | None:
    cursor = conn.execute(
        "SELECT * FROM ipindia_results WHERE id = %s", (search_id,)
    )
    return cursor.fetchone()


def get_ipindia_rows(conn: psycopg.Connection, search_id: int) -> list[dict]:
    cursor = conn.execute(
        """
        SELECT field_key, field_val, row_index
        FROM ipindia_rows
        WHERE search_id = %s
        ORDER BY row_index, field_key
        """,
        (search_id,),
    )
    return cursor.fetchall()


def get_ipindia_searches_by_ids(conn: psycopg.Connection, search_ids: list[int]) -> list[dict]:
    """Fetch multiple ipindia_results rows by id, preserving no particular order."""
    if not search_ids:
        return []
    cursor = conn.execute(
        "SELECT * FROM ipindia_results WHERE id = ANY(%s)",
        (search_ids,),
    )
    return cursor.fetchall()


# ── Series tracker (Auto Discover exhaustive-coverage mode) ──────────────────
# A "series" is a two-digit prefix of the 7-digit application-number space
# ('10'..'99'), covering 100,000 consecutive numbers each. The user maintains
# an ordered queue of series to work through; ipindia_series_progress persists
# per-series scan progress (not per-number — that would be ~7.4M rows at full
# scale) so a sequential scan can resume exactly where it left off across
# app restarts, and so multiple concurrent Auto Discover jobs never claim the
# same series at once.

def _series_range(series: str) -> tuple[int, int]:
    """('78') -> (7800000, 7899999)."""
    base = int(series) * 100_000
    return base, base + 99_999


def save_series_queue(conn: psycopg.Connection, ordered_series: list[str]) -> None:
    """
    Replace the active queue ordering: upserts queue_position (1-indexed by
    list order) for each series in ordered_series, creating a fresh
    status='queued' row with computed range_start/range_end for any series
    never seen before. Series previously queued but omitted from the new
    list have queue_position set to NULL — their status/counts/
    last_checked_number are left untouched, so re-adding one later resumes
    exactly where it left off.
    """
    kept = set(ordered_series)
    conn.execute(
        """
        UPDATE ipindia_series_progress
        SET queue_position = NULL, updated_at = now()
        WHERE queue_position IS NOT NULL AND series != ALL(%s)
        """,
        (list(kept),) if kept else ([],),
    )
    for position, series in enumerate(ordered_series, start=1):
        range_start, range_end = _series_range(series)
        conn.execute(
            """
            INSERT INTO ipindia_series_progress (series, status, queue_position, range_start, range_end)
            VALUES (%(series)s, 'queued', %(position)s, %(range_start)s, %(range_end)s)
            ON CONFLICT (series) DO UPDATE SET
                queue_position = %(position)s,
                updated_at = now()
            """,
            {"series": series, "position": position, "range_start": range_start, "range_end": range_end},
        )
    conn.commit()


def list_series_progress(conn: psycopg.Connection) -> list[dict]:
    """Every series ever touched (currently queued, or with any progress),
    ordered for display: active queue order first, then everything else."""
    cursor = conn.execute(
        """
        SELECT * FROM ipindia_series_progress
        WHERE queue_position IS NOT NULL OR status != 'queued'
        ORDER BY queue_position NULLS LAST, series
        """
    )
    return cursor.fetchall()


def get_series_progress(conn: psycopg.Connection, series: str) -> dict | None:
    """Single series row (any series '10'-'99', even if never queued/touched
    — computes a fresh range_start/range_end on the fly for display purposes
    if no row exists yet, so a not-yet-started series still has a valid
    detail page)."""
    cursor = conn.execute(
        "SELECT * FROM ipindia_series_progress WHERE series = %s",
        (series,),
    )
    row = cursor.fetchone()
    if row:
        return row
    range_start, range_end = _series_range(series)
    return {
        "series": series, "status": "queued", "queue_position": None,
        "range_start": range_start, "range_end": range_end,
        "last_checked_number": None, "hits_count": 0, "misses_count": 0,
        "errors_count": 0, "claimed_by_job_id": None, "email_used": None,
        "started_at": None, "completed_at": None, "updated_at": None,
    }


def claim_next_series(conn: psycopg.Connection, job_id: str, email: str | None = None) -> dict | None:
    """
    Atomically pick and claim the next series for *job_id* to work on:
    prefers an unclaimed in_progress row (resuming after a crash/restart)
    over the lowest queue_position queued row. Among rows with no explicit
    queue_position (i.e. not manually queued), prefers the HIGHEST series
    number first (79, then 78, then 77, ...) so newer series — which tend
    to hold the most recently filed, still-live applications — get scanned
    before older, likely-already-covered ones. An explicit manual queue
    (queue_position set via Save Queue) always takes priority over this
    newest-first fallback. Returns None if nothing is eligible (queue
    empty, or every remaining series is already claimed by a live job).
    """
    cursor = conn.execute(
        """
        UPDATE ipindia_series_progress
        SET status = 'in_progress',
            claimed_by_job_id = %(job_id)s,
            email_used = COALESCE(%(email)s, email_used),
            started_at = COALESCE(started_at, now()),
            updated_at = now()
        WHERE series = (
            SELECT series FROM ipindia_series_progress
            WHERE claimed_by_job_id IS NULL
              AND status IN ('queued', 'in_progress')
            ORDER BY (status = 'in_progress') DESC, queue_position NULLS LAST, series DESC
            LIMIT 1
        )
        RETURNING *
        """,
        {"job_id": job_id, "email": email},
    )
    row = cursor.fetchone()
    conn.commit()
    return row


def claim_specific_series(conn: psycopg.Connection, series: str, job_id: str, email: str | None = None) -> dict | None:
    """
    Atomically claim exactly *series* (creating its row on the fly with
    status='queued' if it's never been touched before), for the "Scan this
    series" button on a series' detail page — a targeted one-off scan,
    independent of queue_position/ordering. Returns None if *series* is
    already claimed by another live job.
    """
    range_start, range_end = _series_range(series)
    conn.execute(
        """
        INSERT INTO ipindia_series_progress (series, status, range_start, range_end)
        VALUES (%(series)s, 'queued', %(range_start)s, %(range_end)s)
        ON CONFLICT (series) DO NOTHING
        """,
        {"series": series, "range_start": range_start, "range_end": range_end},
    )
    cursor = conn.execute(
        """
        UPDATE ipindia_series_progress
        SET status = 'in_progress',
            claimed_by_job_id = %(job_id)s,
            email_used = COALESCE(%(email)s, email_used),
            started_at = COALESCE(started_at, now()),
            updated_at = now()
        WHERE series = %(series)s AND claimed_by_job_id IS NULL
        RETURNING *
        """,
        {"series": series, "job_id": job_id, "email": email},
    )
    row = cursor.fetchone()
    conn.commit()
    return row


def release_series_claim(conn: psycopg.Connection, series: str) -> None:
    """Clear claimed_by_job_id so another job/restart can pick this series
    back up. Called when a job stops (voluntarily or on error) mid-series —
    NOT called on completion (see complete_series)."""
    conn.execute(
        "UPDATE ipindia_series_progress SET claimed_by_job_id = NULL, updated_at = now() WHERE series = %s",
        (series,),
    )
    conn.commit()


def release_all_series_claims(conn: psycopg.Connection) -> int:
    """
    Clear every series claim in the database — call once at app startup.
    _discover_jobs/_discover_stop_events are in-process memory only and
    never survive a restart, so any claimed_by_job_id left over from a
    previous process run is by definition orphaned (its job died with that
    process) and would otherwise block that series from ever being
    resumed. Returns how many claims were cleared.
    """
    cursor = conn.execute(
        "UPDATE ipindia_series_progress SET claimed_by_job_id = NULL, updated_at = now() WHERE claimed_by_job_id IS NOT NULL"
    )
    conn.commit()
    return cursor.rowcount


def update_series_progress(
    conn: psycopg.Connection,
    series: str,
    last_checked_number: int,
    hits_count: int,
    misses_count: int,
    errors_count: int,
) -> None:
    """Checkpoint write. Counts are absolute totals-so-far for this series
    (not deltas), so this is safe to call repeatedly."""
    conn.execute(
        """
        UPDATE ipindia_series_progress
        SET last_checked_number = %(last_checked_number)s,
            hits_count = %(hits_count)s,
            misses_count = %(misses_count)s,
            errors_count = %(errors_count)s,
            updated_at = now()
        WHERE series = %(series)s
        """,
        {
            "series": series,
            "last_checked_number": last_checked_number,
            "hits_count": hits_count,
            "misses_count": misses_count,
            "errors_count": errors_count,
        },
    )
    conn.commit()


def complete_series(conn: psycopg.Connection, series: str) -> None:
    """Mark a series fully scanned: status='done', claim cleared,
    last_checked_number pinned to range_end."""
    conn.execute(
        """
        UPDATE ipindia_series_progress
        SET status = 'done',
            claimed_by_job_id = NULL,
            completed_at = now(),
            last_checked_number = range_end,
            updated_at = now()
        WHERE series = %s
        """,
        (series,),
    )
    conn.commit()


# ── OTP email pool (multiple IMAP inboxes for automatic Auto Discover login) ──
# Lets Auto Discover claim a free email account from a user-managed pool
# (e.g. several Yahoo accounts) instead of requiring one email typed in by
# hand each run, so multiple jobs can run concurrently and log in fully
# automatically (CAPTCHA solve + IMAP OTP read, no manual code entry).
# Mirrors the ipindia_series_progress claim/release pattern above exactly.

def add_pool_account(
    conn: psycopg.Connection,
    email_address: str,
    app_password: str,
    imap_server: str = "imap.gmail.com",
    imap_port: int = 993,
) -> int:
    """Add (or update, if the address already exists) a pool account.
    Returns the row id."""
    cursor = conn.execute(
        """
        INSERT INTO otp_email_pool (email_address, app_password, imap_server, imap_port)
        VALUES (%(email_address)s, %(app_password)s, %(imap_server)s, %(imap_port)s)
        ON CONFLICT (email_address) DO UPDATE SET
            app_password = %(app_password)s,
            imap_server = %(imap_server)s,
            imap_port = %(imap_port)s,
            updated_at = now()
        RETURNING id
        """,
        {
            "email_address": email_address, "app_password": app_password,
            "imap_server": imap_server, "imap_port": imap_port,
        },
    )
    row_id = cursor.fetchone()["id"]
    conn.commit()
    return row_id


def list_pool_accounts(conn: psycopg.Connection) -> list[dict]:
    """Every pool account, newest first — feeds the Settings page list."""
    cursor = conn.execute("SELECT * FROM otp_email_pool ORDER BY id DESC")
    return cursor.fetchall()


def get_pool_account(conn: psycopg.Connection, account_id: int) -> dict | None:
    cursor = conn.execute("SELECT * FROM otp_email_pool WHERE id = %s", (account_id,))
    return cursor.fetchone()


def remove_pool_account(conn: psycopg.Connection, account_id: int) -> None:
    """Delete a pool account. Callers should check it isn't currently
    claimed before calling this (an in-use account shouldn't be yanked out
    from under a running job) — this function itself doesn't guard that."""
    conn.execute("DELETE FROM otp_email_pool WHERE id = %s", (account_id,))
    conn.commit()


def set_pool_account_enabled(conn: psycopg.Connection, account_id: int, enabled: bool) -> None:
    """Pause/resume an account without deleting its saved credentials."""
    conn.execute(
        "UPDATE otp_email_pool SET enabled = %s, updated_at = now() WHERE id = %s",
        (enabled, account_id),
    )
    conn.commit()


def record_pool_test_result(conn: psycopg.Connection, account_id: int, ok: bool, message: str) -> None:
    """Persist the last Test Connection outcome so it's visible on next
    page load, not just within the same test click."""
    conn.execute(
        """
        UPDATE otp_email_pool
        SET last_test_ok = %s, last_test_message = %s, updated_at = now()
        WHERE id = %s
        """,
        (ok, message, account_id),
    )
    conn.commit()


_POOL_ACCOUNT_COOLDOWN_SECONDS = 120

# A claim held longer than this is certainly dead, not just a slow scrape —
# login (CAPTCHA + OTP send + IMAP wait, config.OTP_TIMEOUT_SECONDS) plus the
# longest job-status poll window (tm_manager's _JOB_POLL_TIMEOUT_SECONDS=180)
# together top out well under this. A claim that outlives it means the
# thread that held it died or hung without reaching its `finally:
# release_pool_account(...)` — e.g. killed mid-run by a process
# restart/crash — and with only a handful of pool accounts total, even one
# leaked claim meaningfully shrinks capacity for every other caller
# (tm_manager's auto-fetch included) until this app itself restarts.
_STALE_POOL_CLAIM_SECONDS = 420


def release_stale_pool_claims(conn: psycopg.Connection, machine_id: int | None = None) -> int:
    """Reap pool claims that have been held past _STALE_POOL_CLAIM_SECONDS —
    called periodically (see _auto_launch_loop), not just at startup, so a
    leaked claim during a long-running process doesn't permanently shrink
    the pool until the next restart.

    machine_id: fleet-migration opt-in — when given, only reaps stale
    claims on accounts assigned to THIS machine (or still unassigned).
    Critical once more than one fleet-mode machine shares the DB: without
    this, Machine A calling this unscoped would silently release Machine
    B's own legitimately-held (if slow) claim. None (default, every
    non-fleet call) is byte-identical to before this parameter existed —
    fully global, correct for a lone process where nothing else could
    legitimately be racing it anyway."""
    machine_clause = " AND (assigned_machine_id IS NULL OR assigned_machine_id = %(machine_id)s)" if machine_id is not None else ""
    params = {"stale": _STALE_POOL_CLAIM_SECONDS}
    if machine_id is not None:
        params["machine_id"] = machine_id
    cursor = conn.execute(
        f"""
        UPDATE otp_email_pool
        SET claimed_by_job_id = NULL, updated_at = now()
        WHERE claimed_by_job_id IS NOT NULL
          AND claimed_at <= now() - make_interval(secs => %(stale)s){machine_clause}
        """,
        params,
    )
    conn.commit()
    return cursor.rowcount


def claim_free_pool_account(
    conn: psycopg.Connection, job_id: str, for_batch_worker: bool = False, machine_id: int | None = None,
) -> dict | None:
    """
    Atomically claim a free, enabled pool account for *job_id*. Round-robins
    via ORDER BY last_used_at NULLS FIRST instead of always picking the same
    first row. Excludes accounts used within the last
    _POOL_ACCOUNT_COOLDOWN_SECONDS — IP India's own portal enforces a
    post-logout cooldown on each account (its error messages say so
    explicitly: "already logged in on another tab", "please wait N seconds/
    90 sec as previous logout was not clean") — reusing an account before
    that cooldown clears reliably fails the login rather than saving any
    time, so honoring it here turns those into successful claims elsewhere
    in the pool instead of wasted, doomed-to-fail attempts. Also excludes
    reserved_for IS NOT NULL accounts (the warm session pool - see
    warm_pool.py) so this cold path never steals an account another
    subsystem is keeping permanently logged in. Returns None if every
    eligible account is currently claimed or still cooling down (pool
    exhausted for now, not necessarily empty).

    for_batch_worker=True: this call is from the background batch worker
    (tm_manager's auto-fetch queue), not a real person waiting on a live
    result. When there's more than one free cold account, that's fine — but
    if there's exactly ONE free cold account left, the batch worker is told
    to leave it alone (this call returns None) so a live customer search
    that comes in right after always finds a free account instead of being
    forced into a fresh, slow login. Live-search callers pass the default
    (False) and are allowed to take that last account.

    machine_id: Contabo-coordinator + local-worker-fleet migration — pass
    this ONLY when config.FLEET_MODE_ENABLED is on for this process (a
    worker machine). It adds one extra AND clause restricting eligible
    accounts to ones assigned to this machine (or still unassigned) — see
    otp_email_pool.assigned_machine_id's schema comment. When None (every
    non-fleet call site, unconditionally on Contabo itself since it never
    runs in fleet mode), the SQL is byte-identical to before this
    parameter existed — zero behavior change for any existing install.
    """
    machine_clause = " AND (assigned_machine_id IS NULL OR assigned_machine_id = %(machine_id)s)" if machine_id is not None else ""
    params = {"job_id": job_id, "cooldown": _POOL_ACCOUNT_COOLDOWN_SECONDS}
    if machine_id is not None:
        params["machine_id"] = machine_id

    if for_batch_worker:
        cursor = conn.execute(
            f"""
            UPDATE otp_email_pool
            SET claimed_by_job_id = %(job_id)s,
                claimed_at = now(),
                last_used_at = now(),
                updated_at = now()
            WHERE id = (
                SELECT id FROM otp_email_pool
                WHERE claimed_by_job_id IS NULL AND enabled AND reserved_for IS NULL
                  AND (last_used_at IS NULL OR last_used_at <= now() - make_interval(secs => %(cooldown)s)){machine_clause}
                ORDER BY last_used_at NULLS FIRST, id
                OFFSET 1 LIMIT 1
            )
            RETURNING *
            """,
            params,
        )
    else:
        cursor = conn.execute(
            f"""
            UPDATE otp_email_pool
            SET claimed_by_job_id = %(job_id)s,
                claimed_at = now(),
                last_used_at = now(),
                updated_at = now()
            WHERE id = (
                SELECT id FROM otp_email_pool
                WHERE claimed_by_job_id IS NULL AND enabled AND reserved_for IS NULL
                  AND (last_used_at IS NULL OR last_used_at <= now() - make_interval(secs => %(cooldown)s)){machine_clause}
                ORDER BY last_used_at NULLS FIRST, id
                LIMIT 1
            )
            RETURNING *
            """,
            params,
        )
    row = cursor.fetchone()
    conn.commit()
    return row


def list_warm_pool_accounts(conn: psycopg.Connection, machine_id: int | None = None) -> list[dict]:
    """Every account reserved for the warm session pool (reserved_for =
    'warm_pool') — read at app startup so warm_pool.py knows which accounts
    to keep permanently logged in. Managed via scripts/pool_accounts.py or
    the /api/pool/accounts endpoints.

    machine_id: same fleet-migration opt-in as claim_free_pool_account —
    when given, restricts to accounts assigned to this machine (or still
    unassigned), so two fleet-mode machines can never both pick up the
    same warm account into their in-process warm_pool.py state, even if
    both start at the same instant (the constraint lives in the data, not
    new coordination code). None (default, every non-fleet call) is
    byte-identical to before this parameter existed."""
    if machine_id is not None:
        cursor = conn.execute(
            "SELECT * FROM otp_email_pool WHERE reserved_for = 'warm_pool' AND enabled "
            "AND (assigned_machine_id IS NULL OR assigned_machine_id = %(machine_id)s) ORDER BY id",
            {"machine_id": machine_id},
        )
    else:
        cursor = conn.execute(
            "SELECT * FROM otp_email_pool WHERE reserved_for = 'warm_pool' AND enabled ORDER BY id"
        )
    return cursor.fetchall()


# ── Pool-account role management (dedicated fast lanes vs. cold shared pool) ──
# role 'warm'     -> reserved_for = 'warm_pool'      (App-Number Search — kept
#                     logged in via warm_pool.py, instant fetches)
# role 'wordmark' -> reserved_for = 'wordmark_pool'  (Word Mark + Class Search
#                     — dedicated account(s) app.py's _wm_session caches after
#                     first login, so this pool just keeps them from being
#                     stolen by the shared cold pool / batch auto-fetch)
# role 'cold'     -> reserved_for = NULL             (shared pool — claimed on
#                     demand, fresh login per use, released after)

_ROLE_TO_RESERVED_FOR = {"warm": "warm_pool", "wordmark": "wordmark_pool", "cold": None}


def _role_to_reserved_for(role: str) -> str | None:
    if role not in _ROLE_TO_RESERVED_FOR:
        raise ValueError(f"role must be one of {sorted(_ROLE_TO_RESERVED_FOR)}, got {role!r}")
    return _ROLE_TO_RESERVED_FOR[role]


def upsert_pool_account(
    conn: psycopg.Connection,
    email_address: str,
    app_password: str,
    imap_server: str = "imap.gmail.com",
    imap_port: int = 993,
    role: str = "cold",
) -> dict:
    """Add or update a pool account and set its role in one shot. Returns
    the full row."""
    reserved_for = _role_to_reserved_for(role)
    cursor = conn.execute(
        """
        INSERT INTO otp_email_pool
            (email_address, app_password, imap_server, imap_port, enabled, reserved_for)
        VALUES (%(email)s, %(pw)s, %(imap_server)s, %(imap_port)s, true, %(reserved_for)s)
        ON CONFLICT (email_address) DO UPDATE SET
            app_password = EXCLUDED.app_password,
            imap_server  = EXCLUDED.imap_server,
            imap_port    = EXCLUDED.imap_port,
            reserved_for = EXCLUDED.reserved_for,
            enabled      = true,
            updated_at   = now()
        RETURNING *
        """,
        {
            "email": email_address, "pw": app_password,
            "imap_server": imap_server, "imap_port": imap_port,
            "reserved_for": reserved_for,
        },
    )
    row = cursor.fetchone()
    conn.commit()
    return row


def set_pool_account_role(conn: psycopg.Connection, email_address: str, role: str) -> dict | None:
    """Move an existing account between the warm and cold pools. Returns the
    updated row, or None if no such account."""
    reserved_for = _role_to_reserved_for(role)
    cursor = conn.execute(
        """
        UPDATE otp_email_pool
        SET reserved_for = %(reserved_for)s, updated_at = now()
        WHERE email_address = %(email)s
        RETURNING *
        """,
        {"reserved_for": reserved_for, "email": email_address},
    )
    row = cursor.fetchone()
    conn.commit()
    return row


def set_pool_account_enabled_by_email(conn: psycopg.Connection, email_address: str, enabled: bool) -> dict | None:
    cursor = conn.execute(
        "UPDATE otp_email_pool SET enabled = %s, updated_at = now() WHERE email_address = %s RETURNING *",
        (enabled, email_address),
    )
    row = cursor.fetchone()
    conn.commit()
    return row


def remove_pool_account_by_email(conn: psycopg.Connection, email_address: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM otp_email_pool WHERE email_address = %s", (email_address,)
    )
    conn.commit()
    return cursor.rowcount > 0


def get_pool_account_by_email(conn: psycopg.Connection, email_address: str) -> dict | None:
    cursor = conn.execute(
        "SELECT * FROM otp_email_pool WHERE email_address = %s", (email_address,)
    )
    return cursor.fetchone()


def list_pool_accounts_by_role(conn: psycopg.Connection) -> dict:
    """{'warm': [...], 'wordmark': [...], 'cold': [...]} — for the CLI and
    the /api/pool/accounts endpoint."""
    rows = conn.execute("SELECT * FROM otp_email_pool ORDER BY id").fetchall()
    return {
        "warm": [r for r in rows if r["reserved_for"] == "warm_pool"],
        "wordmark": [r for r in rows if r["reserved_for"] == "wordmark_pool"],
        "cold": [r for r in rows if r["reserved_for"] is None],
    }


def claim_wordmark_pool_account(conn: psycopg.Connection, job_id: str) -> dict | None:
    """Same atomic claim as claim_free_pool_account, but only among accounts
    dedicated to Word Mark + Class search (role='wordmark'). Falls back to
    None (caller then falls back to the shared cold pool) if none are
    assigned or all are already claimed/cooling down — assigning a dedicated
    account is optional, not required."""
    cursor = conn.execute(
        """
        UPDATE otp_email_pool
        SET claimed_by_job_id = %(job_id)s,
            claimed_at = now(),
            last_used_at = now(),
            updated_at = now()
        WHERE id = (
            SELECT id FROM otp_email_pool
            WHERE claimed_by_job_id IS NULL AND enabled AND reserved_for = 'wordmark_pool'
              AND (last_used_at IS NULL OR last_used_at <= now() - make_interval(secs => %(cooldown)s))
            ORDER BY last_used_at NULLS FIRST, id
            LIMIT 1
        )
        RETURNING *
        """,
        {"job_id": job_id, "cooldown": _POOL_ACCOUNT_COOLDOWN_SECONDS},
    )
    row = cursor.fetchone()
    conn.commit()
    return row


def release_pool_account(conn: psycopg.Connection, account_id: int) -> None:
    """Clear the claim so the account becomes available again — called
    whenever a pool-mode job ends (success, stop, or error)."""
    conn.execute(
        "UPDATE otp_email_pool SET claimed_by_job_id = NULL, updated_at = now() WHERE id = %s",
        (account_id,),
    )
    conn.commit()


def release_all_pool_claims(conn: psycopg.Connection, machine_id: int | None = None) -> int:
    """Clear every pool claim — call once at app startup. In-memory job
    tracking never survives a restart, so any claimed_by_job_id left over
    from a previous process run is orphaned. Returns rows cleared.

    machine_id: same fleet-migration opt-in as release_stale_pool_claims —
    when given, only clears claims on accounts assigned to this machine
    (or unassigned), so Machine B restarting can never wipe Machine A's
    in-flight claim on an account assigned exclusively to A. None (default)
    is byte-identical to before — fully global, the only correct behavior
    for a lone non-fleet process."""
    machine_clause = " AND (assigned_machine_id IS NULL OR assigned_machine_id = %(machine_id)s)" if machine_id is not None else ""
    params = {"machine_id": machine_id} if machine_id is not None else {}
    cursor = conn.execute(
        f"UPDATE otp_email_pool SET claimed_by_job_id = NULL, updated_at = now() WHERE claimed_by_job_id IS NOT NULL{machine_clause}",
        params,
    )
    conn.commit()
    return cursor.rowcount


# ── TM-A application PDF-extracted data ──────────────────────────────────────

_TMA_COLUMNS = [
    "app_no", "search_id", "form_type", "source_pdf_filename", "source_pdf_path",
    "temp_ref_no", "fee", "application_filed_as",
    "applicant_name", "applicant_partners", "applicant_address",
    "applicant_address_for_service", "applicant_country", "applicant_jurisdiction",
    "applicant_mobile", "applicant_email", "applicant_nature", "applicant_legal_status",
    "agent_name", "agent_address", "agent_nature", "agent_registration_no",
    "mark_category", "mark_trade_mark", "mark_image_description",
    "language", "class_number", "class_description",
    "use_statement", "other_information",
    "verification_date", "verification_signed_for",
    "receipt_no", "receipt_date", "receipt_amount", "form_no",
    "agent_code", "proprietor_code",
    "application_date", "registry_office",
    "logo_path",
    "raw_json",
    "drive_file_id", "logo_drive_file_id",
    # drive_uploaded_at is deliberately NOT in this list — save_tma_document
    # sets it via SQL now() (or NULL) directly, not through the generic
    # placeholder mechanism this list drives, to avoid Python/DB clock skew.
]


# ── Google Drive migration: filename-keyed lookups for the root scraper's
# own serving routes (ipindia_document/ipindia_logo/ipindia_reminder_document/
# ipindia_correspondence_document) — same matching logic as the identical
# functions in tm_manager/db.py, duplicated here since the two apps don't
# share imports (see tm_manager/db.py's own copies for the full rationale).

def get_tma_document_by_filename(conn: psycopg.Connection, app_no: str, filename: str) -> dict | None:
    """Finds the tma_documents row whose source_pdf_filename matches
    *filename* exactly, newest first (in the rare case of a genuine
    duplicate filename across two extractions)."""
    cursor = conn.execute(
        """
        SELECT drive_file_id, source_pdf_path FROM tma_documents
        WHERE app_no = %(app_no)s AND source_pdf_filename = %(filename)s
        ORDER BY extracted_at DESC LIMIT 1
        """,
        {"app_no": app_no, "filename": filename},
    )
    return cursor.fetchone()


def find_tma_document_by_base_name(conn: psycopg.Connection, app_no: str, base_name: str) -> dict | None:
    """Finds an existing tma_documents row for this app_no whose filename is
    *base_name* itself OR a collision-suffixed variant of it (e.g. base_name
    'tm-a-trade-marks-application.pdf' also matches '...-1.pdf', '...-2.pdf').

    Exists because pdf_processor.py's own collision check only looks at
    local disk presence — with Drive-first uploads, a document already
    saved (on Drive, no local file) looks "new" on every later fetch of the
    same app_no, so it kept re-downloading and re-inserting under a new
    -N suffix every time instead of recognizing it was already saved (see
    the fleet dashboard bug: one app_no showing 4 copies of the same
    document). Checking the database instead of the local filesystem fixes
    this correctly across machines too, since the DB is the one shared
    source of truth a fleet of workers all see."""
    stem, _, ext = base_name.rpartition(".")
    if not stem:
        stem, ext = base_name, ""
    cursor = conn.execute(
        """
        SELECT id, source_pdf_filename, drive_file_id, source_pdf_path FROM tma_documents
        WHERE app_no = %(app_no)s
          AND (source_pdf_filename = %(base_name)s
               OR source_pdf_filename ~ %(pattern)s)
        ORDER BY extracted_at ASC LIMIT 1
        """,
        {
            "app_no": app_no,
            "base_name": base_name,
            "pattern": f"^{re.escape(stem)}-[0-9]+\\.{re.escape(ext)}$" if ext else f"^{re.escape(stem)}-[0-9]+$",
        },
    )
    return cursor.fetchone()


def list_tma_documents(conn: psycopg.Connection, app_no: str) -> list[dict]:
    """All saved application documents for this app_no, newest first — the
    DB-backed replacement for "glob the local disk folder for *.pdf",
    which silently hides any document that uploaded straight to Drive with
    no local file at all."""
    cursor = conn.execute(
        """
        SELECT source_pdf_filename AS filename, extracted_at
        FROM tma_documents
        WHERE app_no = %(app_no)s AND source_pdf_filename IS NOT NULL
        ORDER BY extracted_at DESC
        """,
        {"app_no": app_no},
    )
    return cursor.fetchall()


def get_tma_logo_drive_id(conn: psycopg.Connection, app_no: str, filename: str) -> str | None:
    """Same idea as get_tma_document_by_filename but for the separate logo
    PNG a tma_documents row can reference — matched by the logo's own
    filename, stored in logo_path when it lives locally."""
    cursor = conn.execute(
        """
        SELECT logo_drive_file_id FROM tma_documents
        WHERE app_no = %(app_no)s AND logo_path LIKE %(pattern)s
        ORDER BY extracted_at DESC LIMIT 1
        """,
        {"app_no": app_no, "pattern": f"%{filename}"},
    )
    row = cursor.fetchone()
    return row["logo_drive_file_id"] if row else None


def get_reminder_drive_id(conn: psycopg.Connection, app_no: str, filename: str) -> str | None:
    """document_path is sometimes stored with a folder prefix baked in —
    match on the trailing segment (LIKE) so a bare filename from the URL
    still matches. See tm_manager/db.py's _strip_notice_folder_prefix for
    the read-side normalization this mirrors."""
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


def save_tma_document(
    conn: psycopg.Connection,
    app_no: str,
    search_id: int | None,
    source_pdf_filename: str,
    source_pdf_path: str | None,
    extracted: dict,
    logo_path: str | None = None,
    drive_file_id: str | None = None,
    logo_drive_file_id: str | None = None,
) -> int:
    """Flatten extract_document_data()'s nested dict (TM-A or TM-1 shape)
    and persist it. logo_path, if given, points to a cropped PNG of the
    trademark logo extracted from the source PDF (see
    ipindia_scraper.extract_logo_image), relative to the DATA_DIR.

    source_pdf_path/logo_path can now be None — the Google Drive
    migration's upload hook (pdf_scraper/pdf_processor.py) tries Drive
    first and only falls back to a local file on failure, so a row whose
    document lives on Drive has no local path at all, only drive_file_id/
    logo_drive_file_id. source_pdf_filename is still always set (needed
    by the serving routes to look this row up by filename regardless of
    where the actual bytes live)."""
    applicant = extracted.get("applicant") or {}
    agent = extracted.get("agent") or {}
    mark = extracted.get("mark") or {}
    class_ = extracted.get("class") or {}
    verification = extracted.get("verification") or {}

    row = {
        "app_no": app_no,
        "search_id": search_id,
        "form_type": extracted.get("form_type", "TM-A"),
        "source_pdf_filename": source_pdf_filename,
        "source_pdf_path": source_pdf_path,
        "temp_ref_no": extracted.get("temp_ref_no"),
        "fee": extracted.get("fee"),
        "application_filed_as": extracted.get("application_filed_as"),
        "applicant_name": applicant.get("name"),
        "applicant_partners": applicant.get("partners"),
        "applicant_address": applicant.get("address"),
        "applicant_address_for_service": applicant.get("address_for_service"),
        "applicant_country": applicant.get("country"),
        "applicant_jurisdiction": applicant.get("jurisdiction"),
        "applicant_mobile": applicant.get("mobile"),
        "applicant_email": applicant.get("email"),
        "applicant_nature": applicant.get("nature"),
        "applicant_legal_status": applicant.get("legal_status"),
        "agent_name": agent.get("name"),
        "agent_address": agent.get("address"),
        "agent_nature": agent.get("nature"),
        "agent_registration_no": agent.get("registration_no"),
        "mark_category": mark.get("category"),
        "mark_trade_mark": mark.get("trade_mark"),
        "mark_image_description": mark.get("image_description"),
        "language": extracted.get("language"),
        "class_number": class_.get("number"),
        "class_description": class_.get("description"),
        "use_statement": extracted.get("use_statement"),
        "other_information": extracted.get("other_information"),
        "verification_date": verification.get("date"),
        "verification_signed_for": verification.get("signed_for"),
        "receipt_no": extracted.get("receipt_no"),
        "receipt_date": extracted.get("receipt_date"),
        "receipt_amount": extracted.get("receipt_amount"),
        "form_no": extracted.get("form_no"),
        "agent_code": extracted.get("agent_code"),
        "proprietor_code": extracted.get("proprietor_code"),
        "application_date": extracted.get("application_date"),
        "registry_office": extracted.get("registry_office"),
        "logo_path": logo_path,
        "raw_json": Jsonb(extracted),
        "drive_file_id": drive_file_id,
        "logo_drive_file_id": logo_drive_file_id,
    }

    col_list = ", ".join(_TMA_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in _TMA_COLUMNS)
    # drive_uploaded_at uses Postgres's own now() rather than a Python
    # timestamp (avoids clock-skew), only when a Drive upload actually
    # happened for this row (either file); NULL otherwise (local-only row).
    drive_uploaded_expr = "now()" if (drive_file_id or logo_drive_file_id) else "NULL"
    cur = conn.execute(
        f"""
        INSERT INTO tma_documents ({col_list}, drive_uploaded_at, extracted_at)
        VALUES ({placeholders}, {drive_uploaded_expr}, now())
        RETURNING id
        """,
        row,
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return new_id


def get_tma_document(conn: psycopg.Connection, app_no: str) -> dict | None:
    """Fetch the most recently extracted TM-A document for an application number."""
    cursor = conn.execute(
        """
        SELECT * FROM tma_documents
        WHERE app_no = %s
        ORDER BY extracted_at DESC
        LIMIT 1
        """,
        (app_no,),
    )
    return cursor.fetchone()


# ── Reminders / Correspondence & Notices ──────────────────────────────────────

def save_ipindia_reminder(
    conn: psycopg.Connection, app_no: str, search_id: int | None, row: dict, document_path: str | None
) -> int:
    """
    Upsert on (app_no, corres_no) — re-searching the same application
    refreshes the existing reminder row (e.g. picks up a newly-downloaded
    document_path) instead of inserting a duplicate.
    """
    cur = conn.execute(
        """
        INSERT INTO ipindia_reminders
            (app_no, search_id, s_no, corres_no, corres_date, subject,
             despatch_no, despatch_date, corres_num, document_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (app_no, corres_no) DO UPDATE SET
            search_id = excluded.search_id, s_no = excluded.s_no,
            corres_date = excluded.corres_date, subject = excluded.subject,
            despatch_no = excluded.despatch_no, despatch_date = excluded.despatch_date,
            corres_num = excluded.corres_num,
            document_path = COALESCE(excluded.document_path, ipindia_reminders.document_path),
            fetched_at = now()
        RETURNING id
        """,
        (
            app_no, search_id, row.get("s_no"), row.get("corres_no"), row.get("corres_date"),
            row.get("subject"), row.get("despatch_no"), row.get("despatch_date"),
            row.get("corres_num"), document_path,
        ),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return new_id


def save_ipindia_correspondence(
    conn: psycopg.Connection, app_no: str, search_id: int | None, row: dict, document_path: str | None
) -> int:
    """Upsert on (app_no, corres_no) — see save_ipindia_reminder's docstring."""
    cur = conn.execute(
        """
        INSERT INTO ipindia_correspondence
            (app_no, search_id, s_no, corres_no, corres_date, subject,
             despatch_no, despatch_date, view_url, document_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (app_no, corres_no) DO UPDATE SET
            search_id = excluded.search_id, s_no = excluded.s_no,
            corres_date = excluded.corres_date, subject = excluded.subject,
            despatch_no = excluded.despatch_no, despatch_date = excluded.despatch_date,
            view_url = excluded.view_url,
            document_path = COALESCE(excluded.document_path, ipindia_correspondence.document_path),
            fetched_at = now()
        RETURNING id
        """,
        (
            app_no, search_id, row.get("s_no"), row.get("corres_no"), row.get("corres_date"),
            row.get("subject"), row.get("despatch_no"), row.get("despatch_date"),
            row.get("view_url"), document_path,
        ),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return new_id


def save_ipindia_page_fetch(
    conn: psycopg.Connection, app_no: str, search_id: int | None, kind: str, raw_html: str | None
) -> int:
    """Upsert the raw HTML for one of the two unparsed panels (PR Details or
    the e-main/ViewAppDetails page) on (app_no, kind) — a re-fetch replaces
    the previous raw_html rather than accumulating duplicate rows, matching
    reminders/correspondence's upsert-by-app_no pattern above."""
    cur = conn.execute(
        """
        INSERT INTO ipindia_page_fetches (app_no, search_id, kind, raw_html)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (app_no, kind) DO UPDATE SET
            search_id = excluded.search_id,
            raw_html = excluded.raw_html,
            fetched_at = now()
        RETURNING id
        """,
        (app_no, search_id, kind, raw_html),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return new_id


def get_ipindia_page_fetch(conn: psycopg.Connection, app_no: str, kind: str) -> dict | None:
    cursor = conn.execute(
        "SELECT * FROM ipindia_page_fetches WHERE app_no = %s AND kind = %s",
        (app_no, kind),
    )
    return cursor.fetchone()


def set_trademark_image_path(conn: psycopg.Connection, app_no: str, image_path: str) -> None:
    """Record where a trademark's directly-fetched image was saved on disk —
    see ipindia_trademark_images's schema comment for why this is its own
    table rather than a tma_documents column."""
    conn.execute(
        """
        INSERT INTO ipindia_trademark_images (app_no, image_path, fetched_at)
        VALUES (%s, %s, now())
        ON CONFLICT (app_no) DO UPDATE SET
            image_path = excluded.image_path, fetched_at = now()
        """,
        (app_no, image_path),
    )
    conn.commit()


def get_trademark_image_path(conn: psycopg.Connection, app_no: str) -> str | None:
    cursor = conn.execute(
        "SELECT image_path FROM ipindia_trademark_images WHERE app_no = %s", (app_no,)
    )
    row = cursor.fetchone()
    return row["image_path"] if row else None


def list_ipindia_reminders(conn: psycopg.Connection, app_no: str) -> list[dict]:
    cursor = conn.execute(
        "SELECT * FROM ipindia_reminders WHERE app_no = %s ORDER BY fetched_at ASC, id ASC",
        (app_no,),
    )
    return cursor.fetchall()


def list_ipindia_correspondence(conn: psycopg.Connection, app_no: str) -> list[dict]:
    cursor = conn.execute(
        "SELECT * FROM ipindia_correspondence WHERE app_no = %s ORDER BY fetched_at ASC, id ASC",
        (app_no,),
    )
    return cursor.fetchall()


# ── Work tracking — internal "we're actively handling this application"
# workflow, separate from the portal's own status vocabulary (Objected,
# Formalities Chk Pass, etc.) and separate from the raw scrape data. One
# work_item per app_no; closing it (work_status='Closed') keeps the row and
# its note history rather than deleting, so past work isn't lost — the
# Working page just stops showing it by default. ──────────────────────────

def start_working(conn: psycopg.Connection, app_no: str) -> int:
    """Create (or reactivate, if previously closed) a work item for app_no.
    ON CONFLICT reopens a closed item back to the default status rather than
    erroring — clicking "Start Working" again on something closed earlier
    is a reasonable way to resume it, not a duplicate/no-op to reject."""
    cursor = conn.execute(
        """
        INSERT INTO work_items (app_no)
        VALUES (%(app_no)s)
        ON CONFLICT (app_no) DO UPDATE
            SET closed_at = NULL, updated_at = now()
        RETURNING id
        """,
        {"app_no": app_no},
    )
    work_item_id = cursor.fetchone()["id"]
    conn.commit()
    return work_item_id


def get_work_item_by_app_no(conn: psycopg.Connection, app_no: str) -> dict | None:
    cursor = conn.execute("SELECT * FROM work_items WHERE app_no = %(app_no)s", {"app_no": app_no})
    return cursor.fetchone()


def list_active_work_items(conn: psycopg.Connection) -> list[dict]:
    """Everything currently being worked (closed_at IS NULL), newest activity
    first, each carrying the latest known portal status/word mark/proprietor
    from ipindia_results for display — same "join in the app, not SQL"
    approach as tm_manager's list_trademarks, since data_json needs Python
    unpacking either way."""
    cursor = conn.execute(
        """
        SELECT w.*, r.id AS search_id, r.data_json, t.logo_path
        FROM work_items w
        LEFT JOIN LATERAL (
            SELECT id, data_json FROM ipindia_results
            WHERE query = w.app_no AND search_type IN ('appno', 'appno_discover', 'appno_batch')
            ORDER BY scraped_at DESC
            LIMIT 1
        ) r ON true
        LEFT JOIN LATERAL (
            SELECT logo_path FROM tma_documents
            WHERE app_no = w.app_no
            ORDER BY extracted_at DESC
            LIMIT 1
        ) t ON true
        WHERE w.closed_at IS NULL
        ORDER BY w.updated_at DESC
        """
    )
    rows = cursor.fetchall()
    for row in rows:
        raw = row.pop("data_json", None)
        portal = (raw[0] if raw else {}) or {}
        row["portal_status"] = portal.get("Status")
        row["word_mark"] = portal.get("Trade Mark")
        row["proprietor_name"] = portal.get("Proprietor Name")
        row["logo_filename"] = Path(row["logo_path"]).name if row.get("logo_path") else None
        row["notes"] = list_work_notes(conn, row["id"])
    return rows


def update_work_status(conn: psycopg.Connection, work_item_id: int, work_status: str) -> None:
    conn.execute(
        "UPDATE work_items SET work_status = %(status)s, updated_at = now() WHERE id = %(id)s",
        {"status": work_status, "id": work_item_id},
    )
    conn.commit()


def close_work_item(conn: psycopg.Connection, work_item_id: int) -> None:
    conn.execute(
        "UPDATE work_items SET closed_at = now(), updated_at = now() WHERE id = %(id)s",
        {"id": work_item_id},
    )
    conn.commit()


def add_work_note(conn: psycopg.Connection, work_item_id: int, note: str) -> int:
    cursor = conn.execute(
        "INSERT INTO work_notes (work_item_id, note) VALUES (%(work_item_id)s, %(note)s) RETURNING id",
        {"work_item_id": work_item_id, "note": note},
    )
    note_id = cursor.fetchone()["id"]
    conn.execute("UPDATE work_items SET updated_at = now() WHERE id = %(id)s", {"id": work_item_id})
    conn.commit()
    return note_id


def list_work_notes(conn: psycopg.Connection, work_item_id: int) -> list[dict]:
    cursor = conn.execute(
        "SELECT * FROM work_notes WHERE work_item_id = %(work_item_id)s ORDER BY created_at DESC",
        {"work_item_id": work_item_id},
    )
    return cursor.fetchall()


def delete_work_note(conn: psycopg.Connection, note_id: int) -> None:
    conn.execute("DELETE FROM work_notes WHERE id = %(id)s", {"id": note_id})
    conn.commit()


# ── scrape_jobs — persistent scrape-job state (survives a scraper restart) ────
# The in-memory _ipjobs dict in app.py is the live source of truth while the
# process runs; these functions mirror it into the DB so /api/scrape-status
# still answers correctly after a restart, and so a poller (tm_manager) can
# see a job it started even if the scraper bounced.

def create_scrape_job(
    conn: psycopg.Connection,
    job_id: str,
    app_no: str,
    search_type: str = "appno",
    requested_by: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO scrape_jobs (id, app_no, search_type, status, step, requested_by)
        VALUES (%(id)s, %(app_no)s, %(search_type)s, 'running', 'queued', %(requested_by)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {"id": job_id, "app_no": app_no, "search_type": search_type, "requested_by": requested_by},
    )
    conn.commit()


def set_scrape_job(conn: psycopg.Connection, job_id: str, **fields) -> None:
    """Patch a scrape_jobs row. Accepts: status, step, search_id,
    result_count, error, used_warm, finished_at (pass the literal string
    'now()' to stamp it)."""
    if not fields:
        return
    allowed = {"status", "step", "search_id", "result_count", "error", "used_warm", "finished_at"}
    sets, params = [], {"id": job_id}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "finished_at" and v == "now()":
            sets.append("finished_at = now()")
            continue
        sets.append(f"{k} = %({k})s")
        params[k] = v
    if not sets:
        return
    conn.execute(f"UPDATE scrape_jobs SET {', '.join(sets)} WHERE id = %(id)s", params)
    conn.commit()


def set_scrape_job_from_ipjob(job_id: str, ipjob: dict) -> None:
    """Convenience for app.py's _run_ipindia_job finally-block: take a
    snapshot of the in-memory _ipjobs[job_id] dict and persist the fields we
    care about. Opens its own connection (called from a worker thread)."""
    if not ipjob:
        return
    status = ipjob.get("status") or "running"
    fields = {
        "status": status,
        "step": ipjob.get("step"),
        "search_id": ipjob.get("search_id"),
        "result_count": len(ipjob.get("results") or []) or ipjob.get("result_count"),
        "error": ipjob.get("message") if status == "error" else None,
    }
    if status in ("done", "error"):
        fields["finished_at"] = "now()"
    conn = get_connection()
    try:
        # row may not exist if the job was created before this feature — upsert-ish
        conn.execute(
            "INSERT INTO scrape_jobs (id, app_no, status) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (job_id, ipjob.get("app_no") or "", status),
        )
        conn.commit()
        set_scrape_job(conn, job_id, **{k: v for k, v in fields.items() if v is not None})
    finally:
        conn.close()


def get_scrape_job(conn: psycopg.Connection, job_id: str) -> dict | None:
    return conn.execute("SELECT * FROM scrape_jobs WHERE id = %s", (job_id,)).fetchone()


def get_scrape_jobs(conn: psycopg.Connection, job_ids: list[str]) -> list[dict]:
    if not job_ids:
        return []
    return conn.execute(
        "SELECT * FROM scrape_jobs WHERE id = ANY(%s)", (job_ids,)
    ).fetchall()


def reap_stale_scrape_jobs(conn: psycopg.Connection, older_than_secs: int = 1800) -> int:
    """Mark any still-'running' job older than the cutoff as 'error' — run at
    scraper boot so a job whose thread died on restart doesn't leave a poller
    spinning forever."""
    cursor = conn.execute(
        """
        UPDATE scrape_jobs
        SET status = 'error',
            error = COALESCE(error, 'scrape job was lost (scraper restarted)'),
            finished_at = now()
        WHERE status = 'running'
          AND created_at <= now() - make_interval(secs => %(cut)s)
        """,
        {"cut": older_than_secs},
    )
    conn.commit()
    return cursor.rowcount
