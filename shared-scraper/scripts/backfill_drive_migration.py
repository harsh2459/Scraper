"""
Two-pass Google Drive migration for documents already saved locally —
the existing 46GB backfill, plus any rare local-fallback file that
accumulates after the live upload hook shipped (see pdf_processor.py's
process_pdf, which tries Drive first for NEW documents and only falls
back to local disk on a Drive failure).

Deliberately split into two SEPARATE passes, never fused into one atomic
"upload then delete" step, per the explicit 2026-09-21 decision: local
files must never be auto-deleted the moment they're uploaded — only after
a later, distinct, manually-triggered verification confirms the Drive
copy is actually intact.

  Pass A (--pass upload):   upload every local file that has no
                             drive_file_id yet. Never deletes anything.
  Pass B (--pass delete):   for rows already uploaded (drive_file_id set,
                             local_deleted_at still NULL), re-verify the
                             Drive copy via an MD5 checksum comparison
                             against the local file, and ONLY on a match
                             delete the local file and record
                             local_deleted_at. A mismatch is logged and
                             left alone for manual investigation — never
                             deleted.

Run Pass A first, let it finish (or stop and resume later — every row's
own upload+DB-write is a self-contained, committed unit, so an
interruption only ever loses at most the one row that was mid-flight; a
re-run's `drive_file_id IS NULL` query picks up exactly where it left
off). Run Pass B only once you're satisfied with Pass A's results —
whenever you choose, there's no time pressure, nothing here runs
automatically.

Lives in scripts/ (not scratch/) deliberately — scratch/ is excluded from
every "push everything" deploy per deploy/HOW-TO-UPDATE.md, but this is a
real, deliberately manually-triggered maintenance script meant to travel
with normal deploys, same as scripts/apply_perf_indexes.py or
scripts/pool_accounts.py.

Usage (on the server, from /opt/tmtracker):
    sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/backfill_drive_migration.py --pass upload [--dry-run] [--throttle-seconds 0.75] [--limit N]
    sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/backfill_drive_migration.py --pass delete [--dry-run] [--throttle-seconds 0.75] [--limit N]

--dry-run lists candidates and total size without uploading, verifying, or
deleting anything. --throttle-seconds (default 0.75) pauses between each
file so this never saturates the live production server's bandwidth/disk
I/O — safe to run anytime, just slower. --limit caps how many rows this
run processes (useful for a first small test before a full run).
"""

import argparse
import hashlib
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from folder_export import DATA_DIR, slugify  # noqa: E402
from google_drive import DriveClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_PROGRESS_EVERY = 25

# (table, local_path_column, drive_id_column, kind_prefix, needs_path_fixup)
# kind_prefix matches pdf_processor.py's own "<kind>__<filename>" Drive
# naming convention, so a file uploaded by this backfill and one uploaded
# live by the upload hook end up named identically on the Drive side.
#
# needs_path_fixup=True (reminders/correspondence): document_path in
# Postgres is NOT a usable path as stored — it's sometimes a bare
# filename, sometimes a relative "reminders\<file>"/"correspondence/<file>"
# fragment, depending on when/how it was written historically (same
# inconsistency tm_manager/db.py's _strip_notice_folder_prefix normalizes
# on every read). The real absolute path always follows the same
# DATA_DIR/ipindia/<slug(app_no)>/<kind>/<bare filename> convention the
# serving routes use — _resolve_local_path reconstructs it from that,
# never trusting document_path as a literal path.
#
# needs_path_fixup=False (tma_documents): source_pdf_path is already
# written as a real absolute path by process_pdf — used as-is.
_TMA_TABLE = ("tma_documents", "source_pdf_path", "drive_file_id", "document", False)
_REMINDERS_TABLE = ("ipindia_reminders", "document_path", "drive_file_id", "reminders", True)
_CORRESPONDENCE_TABLE = ("ipindia_correspondence", "document_path", "drive_file_id", "correspondence", True)


def _resolve_local_path(raw_path: str, app_no: str, kind_prefix: str, needs_fixup: bool) -> Path:
    """Turns whatever's actually stored in the DB column into a real,
    absolute local path. See needs_path_fixup's comment above for why
    reminders/correspondence can't just use the column value directly."""
    if not needs_fixup:
        return Path(raw_path)
    bare_filename = re.split(r"[\\/]", raw_path)[-1]
    return DATA_DIR / "ipindia" / slugify(app_no) / kind_prefix / bare_filename


def _mime_for(path: Path) -> str:
    import mimetypes
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _run_upload_pass_for_table(table: str, path_col: str, drive_col: str, kind_prefix: str, needs_fixup: bool,
                                dry_run: bool, throttle: float, limit: int | None) -> dict:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            f"SELECT id, app_no, {path_col} AS local_path FROM {table} "
            f"WHERE {drive_col} IS NULL AND {path_col} IS NOT NULL ORDER BY id"
            + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
    finally:
        conn.close()

    resolved = [(_resolve_local_path(r["local_path"], r["app_no"], kind_prefix, needs_fixup), r) for r in rows]
    total_size = sum(p.stat().st_size for p, _ in resolved if p.is_file())
    logging.info("%s: %d candidate row(s), %.1f MB total", table, len(rows), total_size / 1024 / 1024)
    if dry_run:
        return {"table": table, "candidates": len(rows), "uploaded": 0, "skipped": 0, "errors": 0}

    uploaded = skipped = errors = 0
    for local_path, row in resolved:
        if not local_path.is_file():
            logging.warning("%s id=%s: local file missing on disk (%s) — skipping", table, row["id"], local_path)
            skipped += 1
            continue
        try:
            data = local_path.read_bytes()
            drive_filename = f"{kind_prefix}__{local_path.name}"
            drive = DriveClient()
            if not drive.connect():
                logging.error("%s id=%s: could not connect to Drive — stopping this pass", table, row["id"])
                break
            folder_id = drive.get_or_create_app_folder(row["app_no"])
            if not folder_id:
                logging.error("%s id=%s: could not resolve Drive folder for app_no=%s", table, row["id"], row["app_no"])
                errors += 1
                continue
            file_id = drive.upload_bytes(data, drive_filename, folder_id, _mime_for(local_path))
            if not file_id:
                errors += 1
                continue
        except Exception:
            logging.exception("%s id=%s: upload failed", table, row["id"])
            errors += 1
            continue

        conn = db.get_connection()
        try:
            conn.execute(
                f"UPDATE {table} SET {drive_col} = %(fid)s, drive_uploaded_at = now() WHERE id = %(id)s",
                {"fid": file_id, "id": row["id"]},
            )
            conn.commit()
        finally:
            conn.close()

        uploaded += 1
        if uploaded % _PROGRESS_EVERY == 0:
            logging.info("%s: ... %d uploaded so far", table, uploaded)
        if throttle:
            time.sleep(throttle)

    logging.info("%s: done. uploaded=%d skipped=%d errors=%d", table, uploaded, skipped, errors)
    return {"table": table, "candidates": len(rows), "uploaded": uploaded, "skipped": skipped, "errors": errors}


def _run_delete_pass_for_table(table: str, path_col: str, drive_col: str, kind_prefix: str, needs_fixup: bool,
                                dry_run: bool, throttle: float, limit: int | None) -> dict:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            f"SELECT id, app_no, {path_col} AS local_path, {drive_col} AS drive_file_id FROM {table} "
            f"WHERE {drive_col} IS NOT NULL AND local_deleted_at IS NULL AND {path_col} IS NOT NULL "
            f"ORDER BY id" + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
    finally:
        conn.close()

    logging.info("%s: %d row(s) uploaded but not yet locally cleaned up", table, len(rows))
    if dry_run:
        return {"table": table, "candidates": len(rows), "deleted": 0, "mismatched": 0, "errors": 0}

    deleted = mismatched = errors = 0
    for row in rows:
        local_path = _resolve_local_path(row["local_path"], row["app_no"], kind_prefix, needs_fixup)
        if not local_path.is_file():
            # Already gone locally somehow (e.g. a prior interrupted run) —
            # just record it as cleaned up, nothing left to verify/delete.
            conn = db.get_connection()
            try:
                conn.execute(f"UPDATE {table} SET local_deleted_at = now() WHERE id = %(id)s", {"id": row["id"]})
                conn.commit()
            finally:
                conn.close()
            continue
        try:
            drive = DriveClient()
            if not drive.connect():
                logging.error("%s id=%s: could not connect to Drive — stopping this pass", table, row["id"])
                break
            meta = drive.get_file_metadata(row["drive_file_id"])
            if not meta or not meta.get("md5Checksum"):
                logging.error("%s id=%s: could not fetch Drive metadata/checksum — leaving local file in place", table, row["id"])
                errors += 1
                continue
            local_md5 = hashlib.md5(local_path.read_bytes()).hexdigest()
            if local_md5 != meta["md5Checksum"]:
                logging.error(
                    "%s id=%s: MD5 MISMATCH (local=%s drive=%s) — leaving local file in place for manual review",
                    table, row["id"], local_md5, meta["md5Checksum"],
                )
                mismatched += 1
                continue
        except Exception:
            logging.exception("%s id=%s: verification failed", table, row["id"])
            errors += 1
            continue

        try:
            local_path.unlink()
        except Exception:
            logging.exception("%s id=%s: verified OK but could not delete local file %s", table, row["id"], local_path)
            errors += 1
            continue

        conn = db.get_connection()
        try:
            conn.execute(f"UPDATE {table} SET local_deleted_at = now() WHERE id = %(id)s", {"id": row["id"]})
            conn.commit()
        finally:
            conn.close()

        deleted += 1
        if deleted % _PROGRESS_EVERY == 0:
            logging.info("%s: ... %d deleted so far", table, deleted)
        if throttle:
            time.sleep(throttle)

    logging.info("%s: done. deleted=%d mismatched=%d errors=%d", table, deleted, mismatched, errors)
    return {"table": table, "candidates": len(rows), "deleted": deleted, "mismatched": mismatched, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pass", dest="pass_name", choices=["upload", "delete"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--throttle-seconds", type=float, default=0.75)
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of rows processed per table (for a small first test).")
    args = parser.parse_args()

    import config
    if not config.is_drive_configured():
        print("GOOGLE_DRIVE_* credentials are not configured — nothing to do. See scripts/google_drive_auth_setup.py.")
        sys.exit(1)

    tables = [_TMA_TABLE, _REMINDERS_TABLE, _CORRESPONDENCE_TABLE]
    results = []
    if args.pass_name == "upload":
        for table, path_col, drive_col, kind_prefix, needs_fixup in tables:
            results.append(_run_upload_pass_for_table(
                table, path_col, drive_col, kind_prefix, needs_fixup,
                args.dry_run, args.throttle_seconds, args.limit,
            ))
    else:
        for table, path_col, drive_col, kind_prefix, needs_fixup in tables:
            results.append(_run_delete_pass_for_table(
                table, path_col, drive_col, kind_prefix, needs_fixup,
                args.dry_run, args.throttle_seconds, args.limit,
            ))

    print()
    print(f"=== Pass '{args.pass_name}' summary{' (DRY RUN — nothing changed)' if args.dry_run else ''} ===")
    for r in results:
        print(f"  {r}")


if __name__ == "__main__":
    main()
