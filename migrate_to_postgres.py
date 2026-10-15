"""
One-time migration: copy data from the legacy quickcompany.db (SQLite) into
the new PostgreSQL database, and backfill the tma_documents table from the
existing data/ipindia/*/*.json sidecar files written by earlier PDF extraction.

Run once, manually:
    python migrate_to_postgres.py [--force]

--force allows re-running against a Postgres database that already has data
(normally the script refuses to avoid inserting duplicates).
"""

import json
import sqlite3
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

import config
import db as pg_db
from folder_export import DATA_DIR, slugify

SQLITE_PATH = Path(__file__).parent / "quickcompany.db"


def _pg_connect() -> psycopg.Connection:
    return psycopg.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        dbname=config.PG_DBNAME,
        user=config.PG_USER,
        password=config.PG_PASSWORD or None,
    )


def _table_count(conn: psycopg.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _reset_sequence(conn: psycopg.Connection, table: str) -> None:
    conn.execute(
        f"""
        SELECT setval(
            pg_get_serial_sequence(%s, 'id'),
            (SELECT COALESCE(MAX(id), 1) FROM {table})
        )
        """,
        (table,),
    )


def migrate_core_tables(force: bool) -> None:
    if not SQLITE_PATH.exists():
        print(f"No SQLite database found at {SQLITE_PATH} — skipping core table migration.")
        return

    sconn = sqlite3.connect(SQLITE_PATH)
    sconn.row_factory = sqlite3.Row

    pconn = _pg_connect()
    # Ensure tables exist (same schema as db.py's get_connection()).
    pconn.execute(pg_db.TRADEMARK_SCHEMA)
    pconn.execute(pg_db.IPINDIA_SCHEMA)
    pconn.execute(pg_db.IPINDIA_ROW_SCHEMA)
    pconn.execute(pg_db.TMA_DOCUMENTS_SCHEMA)
    pconn.commit()

    existing_counts = {
        "trademarks": _table_count(pconn, "trademarks"),
        "ipindia_results": _table_count(pconn, "ipindia_results"),
        "ipindia_rows": _table_count(pconn, "ipindia_rows"),
    }
    if any(existing_counts.values()) and not force:
        print("Target Postgres tables already contain data — refusing to re-run migration.")
        print(f"  Existing counts: {existing_counts}")
        print("  Pass --force to migrate anyway (may create duplicates).")
        sconn.close()
        pconn.close()
        return

    # ── trademarks ──
    rows = sconn.execute("SELECT * FROM trademarks").fetchall()
    columns = rows[0].keys() if rows else []
    for row in rows:
        cols = list(columns)
        placeholders = ", ".join(f"%({c})s" for c in cols)
        col_list = ", ".join(cols)
        pconn.execute(
            f"INSERT INTO trademarks ({col_list}) OVERRIDING SYSTEM VALUE VALUES ({placeholders})",
            dict(row),
        )
    pconn.commit()
    print(f"Migrated {len(rows)} row(s) into trademarks.")

    # ── ipindia_results ──
    results_rows = sconn.execute("SELECT * FROM ipindia_results").fetchall()
    for row in results_rows:
        d = dict(row)
        data_json = d.get("data_json")
        parsed = None
        if data_json:
            try:
                parsed = json.loads(data_json)
            except Exception:
                parsed = None
        pconn.execute(
            """
            INSERT INTO ipindia_results (id, query, search_type, email_used, data_json, error, scraped_at)
            OVERRIDING SYSTEM VALUE
            VALUES (%(id)s, %(query)s, %(search_type)s, %(email_used)s, %(data_json)s, %(error)s, %(scraped_at)s)
            """,
            {
                "id": d["id"],
                "query": d["query"],
                "search_type": d["search_type"],
                "email_used": d["email_used"],
                "data_json": Jsonb(parsed) if parsed is not None else None,
                "error": d["error"],
                "scraped_at": d["scraped_at"],
            },
        )
    pconn.commit()
    print(f"Migrated {len(results_rows)} row(s) into ipindia_results.")

    # ── ipindia_rows ──
    row_rows = sconn.execute("SELECT * FROM ipindia_rows").fetchall()
    for row in row_rows:
        d = dict(row)
        pconn.execute(
            """
            INSERT INTO ipindia_rows (id, search_id, field_key, field_val, row_index)
            OVERRIDING SYSTEM VALUE
            VALUES (%(id)s, %(search_id)s, %(field_key)s, %(field_val)s, %(row_index)s)
            """,
            d,
        )
    pconn.commit()
    print(f"Migrated {len(row_rows)} row(s) into ipindia_rows.")

    # ── reset identity sequences so future inserts don't collide ──
    for table in ("trademarks", "ipindia_results", "ipindia_rows"):
        _reset_sequence(pconn, table)
    pconn.commit()
    print("Reset identity sequences for trademarks, ipindia_results, ipindia_rows.")

    sconn.close()
    pconn.close()


def backfill_tma_documents(force: bool) -> None:
    pconn = _pg_connect()
    existing = _table_count(pconn, "tma_documents")
    if existing and not force:
        print(f"tma_documents already has {existing} row(s) — skipping backfill (use --force to redo).")
        pconn.close()
        return

    ipindia_dir = DATA_DIR / "ipindia"
    if not ipindia_dir.is_dir():
        print(f"No {ipindia_dir} directory found — skipping tma_documents backfill.")
        pconn.close()
        return

    json_paths = sorted(ipindia_dir.glob("*/*.json"))
    saved = 0
    skipped = 0
    for json_path in json_paths:
        app_no = json_path.parent.name
        try:
            with open(json_path, encoding="utf-8") as f:
                extracted = json.load(f)
        except Exception as exc:
            print(f"  SKIP {json_path}: could not parse JSON ({exc})")
            skipped += 1
            continue

        pdf_path = json_path.with_suffix(".pdf")

        match = pconn.execute(
            """
            SELECT id FROM ipindia_results
            WHERE search_type IN ('appno', 'appno_discover') AND query = %s
            ORDER BY scraped_at DESC
            LIMIT 1
            """,
            (app_no,),
        ).fetchone()
        search_id = match[0] if match else None

        applicant = extracted.get("applicant") or {}
        agent = extracted.get("agent") or {}
        mark = extracted.get("mark") or {}
        class_ = extracted.get("class") or {}
        verification = extracted.get("verification") or {}

        pconn.execute(
            """
            INSERT INTO tma_documents (
                app_no, search_id, source_pdf_filename, source_pdf_path,
                temp_ref_no, fee, application_filed_as,
                applicant_name, applicant_partners, applicant_address,
                applicant_address_for_service, applicant_country, applicant_jurisdiction,
                applicant_mobile, applicant_email, applicant_nature, applicant_legal_status,
                agent_name, agent_address, agent_nature, agent_registration_no,
                mark_category, mark_trade_mark, mark_image_description,
                language, class_number, class_description,
                use_statement, other_information,
                verification_date, verification_signed_for,
                raw_json
            ) VALUES (
                %(app_no)s, %(search_id)s, %(source_pdf_filename)s, %(source_pdf_path)s,
                %(temp_ref_no)s, %(fee)s, %(application_filed_as)s,
                %(applicant_name)s, %(applicant_partners)s, %(applicant_address)s,
                %(applicant_address_for_service)s, %(applicant_country)s, %(applicant_jurisdiction)s,
                %(applicant_mobile)s, %(applicant_email)s, %(applicant_nature)s, %(applicant_legal_status)s,
                %(agent_name)s, %(agent_address)s, %(agent_nature)s, %(agent_registration_no)s,
                %(mark_category)s, %(mark_trade_mark)s, %(mark_image_description)s,
                %(language)s, %(class_number)s, %(class_description)s,
                %(use_statement)s, %(other_information)s,
                %(verification_date)s, %(verification_signed_for)s,
                %(raw_json)s
            )
            """,
            {
                "app_no": app_no,
                "search_id": search_id,
                "source_pdf_filename": pdf_path.name if pdf_path.exists() else None,
                "source_pdf_path": str(pdf_path) if pdf_path.exists() else None,
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
                "raw_json": Jsonb(extracted),
            },
        )
        saved += 1

    pconn.commit()
    pconn.close()
    print(f"Backfilled {saved} tma_documents row(s) ({skipped} skipped due to parse errors).")


def main() -> None:
    force = "--force" in sys.argv

    # Step 1: ensure the target database exists (must run outside a transaction).
    maint_conn = psycopg.connect(
        host=config.PG_HOST, port=config.PG_PORT, dbname="postgres",
        user=config.PG_USER, password=config.PG_PASSWORD or None,
        autocommit=True,
    )
    exists = maint_conn.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (config.PG_DBNAME,)
    ).fetchone()
    if not exists:
        maint_conn.execute(f'CREATE DATABASE "{config.PG_DBNAME}" OWNER {config.PG_USER}')
        print(f"Created database {config.PG_DBNAME!r}.")
    else:
        print(f"Database {config.PG_DBNAME!r} already exists.")
    maint_conn.close()

    migrate_core_tables(force)
    backfill_tma_documents(force)
    print("Migration complete.")


if __name__ == "__main__":
    main()
