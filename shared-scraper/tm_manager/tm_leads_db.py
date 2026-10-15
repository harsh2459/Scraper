"""
PostgreSQL storage for the TM Lead & Invoice system.

Separate module from db.py (which owns the portfolio-tracking tables) since
leads/consultants/packages/invoices are a distinct feature area with their
own lifecycle — kept here so db.py doesn't grow unrelated concerns. Shares
the same "datascraper" Postgres connection helper conventions as db.py.
"""

import threading

import psycopg
from psycopg.rows import dict_row

import config

TM_CONSULTANTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm_consultants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    mobile          TEXT,
    commission_rate NUMERIC(5,2) DEFAULT 0,
    agreement_type  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

TM_PACKAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm_packages (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT NOT NULL,
    description      TEXT,
    includes         TEXT,
    excludes         TEXT,
    government_fee   NUMERIC(10,2) DEFAULT 0,
    professional_fee NUMERIC(10,2) DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

TM_SYSTEM_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm_system_settings (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key        TEXT NOT NULL UNIQUE,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DEFAULT_SETTINGS = {
    "company_name": "ProtectMyBrand",
    "company_address_1": "123 IP Tower",
    "company_address_2": "New Delhi",
    "company_city": "New Delhi - 110001",
    "company_email": "info@protectmybrand.in",
    "company_logo": None,
}

# id kept a plain SERIAL/IDENTITY (not UUID) purely so invoice numbers can
# read as "#INV-<year>-<id>" — a short human-facing number — without a
# second counter table; every other new table here uses UUIDs like the rest
# of the schema since they're never shown to a client.
TM_LEADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm_leads (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    name               TEXT NOT NULL,
    mobile             TEXT,
    business_name      TEXT,
    proposed_trademark TEXT,
    class              TEXT,
    source             TEXT,
    reference_name     TEXT,
    lead_date          DATE DEFAULT CURRENT_DATE,
    assigned_to        TEXT,
    entity_type        TEXT,
    state              TEXT,

    qualification_status TEXT,
    qualification_notes  TEXT,

    consultant_id UUID REFERENCES tm_consultants(id) ON DELETE SET NULL,

    pricing        NUMERIC(10,2) DEFAULT 6500,
    package        TEXT,
    payment_status TEXT NOT NULL DEFAULT 'Payment Pending',

    partnership_deed_at    TIMESTAMPTZ,
    firm_pan_at            TIMESTAMPTZ,
    partner_kyc_at         TIMESTAMPTZ,
    udyam_reg_at           TIMESTAMPTZ,
    brand_logo_at          TIMESTAMPTZ,
    firm_email_verified_at TIMESTAMPTZ,
    poa_drafted_at         TIMESTAMPTZ,
    stamp_purchased_at     TIMESTAMPTZ,
    poa_signed_at          TIMESTAMPTZ,
    tma_drafted_at         TIMESTAMPTZ,
    tma_shared_at          TIMESTAMPTZ,
    invoice_generated_at   TIMESTAMPTZ,
    final_payment_at       TIMESTAMPTZ,

    partnership_deed_path  TEXT,
    firm_pan_path          TEXT,
    partner_kyc_path       TEXT,
    udyam_reg_path         TEXT,
    brand_logo_path        TEXT,
    firm_email_path        TEXT,
    poa_drafted_path       TEXT,
    stamp_purchased_path   TEXT,
    poa_signed_path        TEXT,
    tma_drafted_path       TEXT,
    tma_shared_path        TEXT,
    invoice_generated_path TEXT,
    final_payment_path     TEXT,

    doc_business_proof BOOLEAN NOT NULL DEFAULT FALSE,
    doc_pan_card       BOOLEAN NOT NULL DEFAULT FALSE,
    doc_id_proof       BOOLEAN NOT NULL DEFAULT FALSE,
    doc_brand_logo     BOOLEAN NOT NULL DEFAULT FALSE,

    arn_number       TEXT,
    application_no   TEXT,
    filed_at         TIMESTAMPTZ,

    arn_receipt_path  TEXT,
    fees_receipt_path TEXT,
    e_main_page_path  TEXT,
    draft_path        TEXT,

    vienna_codification_at TIMESTAMPTZ,
    formalities_check_at   TIMESTAMPTZ,
    mark_status            TEXT,
    mark_status_date       DATE,
    action_by_us           TEXT,
    status_comment         TEXT,
    reply_status           TEXT,

    reply_filed_at TIMESTAMPTZ,
    hearing_date   DATE,

    journal_published_at     TIMESTAMPTZ,
    journal_number            TEXT,
    journal_publication_path TEXT,

    registered_at                 TIMESTAMPTZ,
    certificate_number            TEXT,
    registration_certificate_path TEXT,

    invoice_package_name        TEXT DEFAULT 'TRADEMARK APPLICATION',
    invoice_package_description TEXT DEFAULT 'TM APPLICATION FEES',
    invoice_items                JSONB,
    invoice_remarks              TEXT,

    status     TEXT NOT NULL DEFAULT 'New Lead',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tm_leads_status ON tm_leads(status);
CREATE INDEX IF NOT EXISTS idx_tm_leads_consultant ON tm_leads(consultant_id);
CREATE INDEX IF NOT EXISTS idx_tm_leads_payment_status ON tm_leads(payment_status);
CREATE INDEX IF NOT EXISTS idx_tm_leads_created_at ON tm_leads(created_at DESC);
"""

TM_LEAD_ACTIVITIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm_lead_activities (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id      INTEGER NOT NULL REFERENCES tm_leads(id) ON DELETE CASCADE,
    type         TEXT NOT NULL,
    description  TEXT NOT NULL,
    performed_by TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tm_lead_activities_lead ON tm_lead_activities(lead_id);
"""

_schema_ready = False
_schema_lock = threading.Lock()


def _ensure_leads_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        conn.execute(TM_CONSULTANTS_SCHEMA)
        conn.execute(TM_PACKAGES_SCHEMA)
        conn.execute(TM_SYSTEM_SETTINGS_SCHEMA)
        conn.execute(TM_LEADS_SCHEMA)
        conn.execute(TM_LEAD_ACTIVITIES_SCHEMA)
        for key, value in _DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO tm_system_settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO NOTHING",
                (key, value),
            )
        conn.commit()
        _schema_ready = True


def get_connection() -> psycopg.Connection:
    """Borrows from tm_manager/db.py's shared pool (same DB) so we don't run a
    second pool. Ensures the leads-specific tables exist on first use."""
    import db as _tmdb
    conn = _tmdb.get_connection()
    _ensure_leads_schema(conn)
    return conn


# ─── Kanban stage computation ─────────────────────────────────────────────
# Mirrors the "18 Kanban Stages" table from the plan doc — evaluated in this
# exact order since a lead can satisfy several conditions at once (e.g. a
# registered lead also has payment_status = 'Payment Done') and only the
# furthest-along stage should win.
KANBAN_STAGES = [
    "New Leads",
    "Verified",
    "Consulting",
    "Payment Done",
    "Docs Ready",
    "App Filed",
    "Recently Applied",
    "Marked for Exam",
    "Formality Pass",
    "Formality Fail",
    "Objection/Opp.",
    "Reply Filed",
    "Advertised",
    "Hearing",
    "TM-M Filed",
    "Exam Pending",
    "Exam Report",
    "Registered",
]


def compute_kanban_stage(lead: dict) -> str:
    status = (lead.get("status") or "").strip()
    if status == "Registered":
        return "Registered"
    if status == "Examination Report File":
        return "Exam Report"
    if status == "Pending for Examination":
        return "Exam Pending"
    if status == "TM-M File":
        return "TM-M Filed"
    if status == "Hearing":
        return "Hearing"
    if status in ("Accepted & Advertised", "Published"):
        return "Advertised"
    if status == "Reply Filed":
        return "Reply Filed"
    if status in ("Objection", "Opposition"):
        return "Objection/Opp."
    if status == "Formality Check Fail":
        return "Formality Fail"
    if status == "Formality Check Pass":
        return "Formality Pass"
    if status == "Marked for Exam":
        return "Marked for Exam"
    if status == "Recently Applied":
        return "Recently Applied"
    if lead.get("arn_number") or status == "Filed":
        return "App Filed"
    if lead.get("final_payment_at"):
        return "Docs Ready"
    if lead.get("payment_status") == "Payment Done":
        return "Payment Done"
    if lead.get("consultant_id") or (lead.get("source") or "") != "Referral":
        return "Consulting"
    if lead.get("qualification_status"):
        return "Verified"
    return "New Leads"


# ─── Leads CRUD ────────────────────────────────────────────────────────────

_LEAD_LIST_FIELDS = (
    "id, name, mobile, business_name, proposed_trademark, class, source, "
    "reference_name, lead_date, assigned_to, entity_type, state, "
    "qualification_status, consultant_id, pricing, package, payment_status, "
    "final_payment_at, arn_number, application_no, filed_at, mark_status, "
    "hearing_date, journal_number, registered_at, certificate_number, status, "
    "created_at, updated_at"
)


def list_leads(conn: psycopg.Connection, filters: dict | None = None) -> list[dict]:
    filters = filters or {}
    clauses = []
    params: list = []

    if filters.get("status"):
        clauses.append("status = %s")
        params.append(filters["status"])
    if filters.get("consultant_id"):
        clauses.append("consultant_id = %s")
        params.append(filters["consultant_id"])
    if filters.get("class"):
        clauses.append("class = %s")
        params.append(filters["class"])
    if filters.get("assigned_to"):
        clauses.append("assigned_to = %s")
        params.append(filters["assigned_to"])
    if filters.get("payment_status"):
        clauses.append("payment_status = %s")
        params.append(filters["payment_status"])
    if filters.get("date_from"):
        clauses.append("lead_date >= %s")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("lead_date <= %s")
        params.append(filters["date_to"])
    if filters.get("q"):
        clauses.append(
            "(name ILIKE %s OR mobile ILIKE %s OR business_name ILIKE %s OR "
            "proposed_trademark ILIKE %s OR arn_number ILIKE %s OR application_no ILIKE %s)"
        )
        like = f"%{filters['q']}%"
        params.extend([like, like, like, like, like, like])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT {_LEAD_LIST_FIELDS} FROM tm_leads {where} ORDER BY created_at DESC",
        params,
    ).fetchall()
    for row in rows:
        row["kanban_stage"] = compute_kanban_stage(row)
    return rows


def get_lead(conn: psycopg.Connection, lead_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM tm_leads WHERE id = %s", (lead_id,)).fetchone()
    if row:
        row["kanban_stage"] = compute_kanban_stage(row)
    return row


def create_lead(conn: psycopg.Connection, fields: dict) -> dict:
    allowed = {
        "name", "mobile", "business_name", "proposed_trademark", "class", "source",
        "reference_name", "lead_date", "assigned_to", "entity_type", "state",
    }
    data = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "name" not in data:
        raise ValueError("name is required")

    cols = list(data.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    row = conn.execute(
        f"INSERT INTO tm_leads ({col_list}) VALUES ({placeholders}) RETURNING *",
        [data[c] for c in cols],
    ).fetchone()
    conn.commit()
    log_activity(conn, row["id"], "Status Update", f"Lead created for {row['name']}")
    row["kanban_stage"] = compute_kanban_stage(row)
    return row


def update_lead(conn: psycopg.Connection, lead_id: int, fields: dict) -> dict | None:
    allowed = {
        "name", "mobile", "business_name", "proposed_trademark", "class", "source",
        "reference_name", "lead_date", "assigned_to", "entity_type", "state", "status",
    }
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return get_lead(conn, lead_id)

    set_clause = ", ".join(f"{k} = %s" for k in data)
    row = conn.execute(
        f"UPDATE tm_leads SET {set_clause}, updated_at = now() WHERE id = %s RETURNING *",
        [*data.values(), lead_id],
    ).fetchone()
    conn.commit()
    if row:
        row["kanban_stage"] = compute_kanban_stage(row)
    return row


def delete_lead(conn: psycopg.Connection, lead_id: int) -> None:
    conn.execute("DELETE FROM tm_leads WHERE id = %s", (lead_id,))
    conn.commit()


def set_lead_fields(conn: psycopg.Connection, lead_id: int, fields: dict, allowed: set[str]) -> dict | None:
    """Shared helper for the sub-action endpoints (qualify, map-consultant,
    update-payment, etc.) — each only ever writes its own small slice of
    columns, so this keeps every one of those handlers to a single call
    instead of duplicating the same UPDATE-building logic per action."""
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return get_lead(conn, lead_id)
    set_clause = ", ".join(f"{k} = %s" for k in data)
    row = conn.execute(
        f"UPDATE tm_leads SET {set_clause}, updated_at = now() WHERE id = %s RETURNING *",
        [*data.values(), lead_id],
    ).fetchone()
    conn.commit()
    if row:
        row["kanban_stage"] = compute_kanban_stage(row)
    return row


def kanban_grouped(conn: psycopg.Connection) -> dict:
    rows = list_leads(conn)
    grouped: dict[str, list] = {stage: [] for stage in KANBAN_STAGES}
    for row in rows:
        grouped.setdefault(row["kanban_stage"], []).append(row)
    return grouped


# ─── Activity log ──────────────────────────────────────────────────────────

def log_activity(conn: psycopg.Connection, lead_id: int, type_: str, description: str, performed_by: str | None = None) -> None:
    conn.execute(
        "INSERT INTO tm_lead_activities (lead_id, type, description, performed_by) VALUES (%s, %s, %s, %s)",
        (lead_id, type_, description, performed_by),
    )
    conn.commit()


def list_activities(conn: psycopg.Connection, lead_id: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM tm_lead_activities WHERE lead_id = %s ORDER BY created_at DESC",
        (lead_id,),
    ).fetchall()


# ─── Consultants CRUD ──────────────────────────────────────────────────────

def list_consultants(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM tm_consultants ORDER BY name").fetchall()


def create_consultant(conn: psycopg.Connection, fields: dict) -> dict:
    row = conn.execute(
        "INSERT INTO tm_consultants (name, mobile, commission_rate, agreement_type) "
        "VALUES (%s, %s, %s, %s) RETURNING *",
        (fields.get("name"), fields.get("mobile"), fields.get("commission_rate", 0), fields.get("agreement_type")),
    ).fetchone()
    conn.commit()
    return row


def update_consultant(conn: psycopg.Connection, consultant_id: str, fields: dict) -> dict | None:
    allowed = {"name", "mobile", "commission_rate", "agreement_type"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return conn.execute("SELECT * FROM tm_consultants WHERE id = %s", (consultant_id,)).fetchone()
    set_clause = ", ".join(f"{k} = %s" for k in data)
    row = conn.execute(
        f"UPDATE tm_consultants SET {set_clause} WHERE id = %s RETURNING *",
        [*data.values(), consultant_id],
    ).fetchone()
    conn.commit()
    return row


def delete_consultant(conn: psycopg.Connection, consultant_id: str) -> None:
    conn.execute("DELETE FROM tm_consultants WHERE id = %s", (consultant_id,))
    conn.commit()


# ─── Packages CRUD ─────────────────────────────────────────────────────────

def list_packages(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM tm_packages ORDER BY name").fetchall()


def create_package(conn: psycopg.Connection, fields: dict) -> dict:
    row = conn.execute(
        "INSERT INTO tm_packages (name, description, includes, excludes, government_fee, professional_fee) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
        (
            fields.get("name"), fields.get("description"), fields.get("includes"),
            fields.get("excludes"), fields.get("government_fee", 0), fields.get("professional_fee", 0),
        ),
    ).fetchone()
    conn.commit()
    return row


def update_package(conn: psycopg.Connection, package_id: str, fields: dict) -> dict | None:
    allowed = {"name", "description", "includes", "excludes", "government_fee", "professional_fee"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return conn.execute("SELECT * FROM tm_packages WHERE id = %s", (package_id,)).fetchone()
    set_clause = ", ".join(f"{k} = %s" for k in data)
    row = conn.execute(
        f"UPDATE tm_packages SET {set_clause} WHERE id = %s RETURNING *",
        [*data.values(), package_id],
    ).fetchone()
    conn.commit()
    return row


def delete_package(conn: psycopg.Connection, package_id: str) -> None:
    conn.execute("DELETE FROM tm_packages WHERE id = %s", (package_id,))
    conn.commit()


# ─── Settings ──────────────────────────────────────────────────────────────

def get_settings(conn: psycopg.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM tm_system_settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def update_settings(conn: psycopg.Connection, values: dict) -> dict:
    for key, value in values.items():
        conn.execute(
            "INSERT INTO tm_system_settings (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, value),
        )
    conn.commit()
    return get_settings(conn)


# ─── Dashboard stats ───────────────────────────────────────────────────────

def dashboard_stats(conn: psycopg.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) AS n FROM tm_leads").fetchone()["n"]
    payment_done = conn.execute(
        "SELECT COUNT(*) AS n FROM tm_leads WHERE payment_status = 'Payment Done'"
    ).fetchone()["n"]
    registered = conn.execute("SELECT COUNT(*) AS n FROM tm_leads WHERE status = 'Registered'").fetchone()["n"]
    revenue = conn.execute(
        "SELECT COALESCE(SUM(pricing), 0) AS total FROM tm_leads WHERE payment_status = 'Payment Done'"
    ).fetchone()["total"]
    stage_counts = kanban_grouped(conn)
    stage_summary = [{"stage": stage, "count": len(rows)} for stage, rows in stage_counts.items()]
    recent_activities = conn.execute(
        "SELECT a.*, l.name AS lead_name FROM tm_lead_activities a "
        "JOIN tm_leads l ON l.id = a.lead_id ORDER BY a.created_at DESC LIMIT 15"
    ).fetchall()
    return {
        "total_leads": total,
        "payment_done_count": payment_done,
        "registered_count": registered,
        "total_revenue": revenue,
        "stage_summary": stage_summary,
        "recent_activities": recent_activities,
    }
