"""
One-time repair for tma_documents.source_pdf_path rows that still hold the
original local Windows path (e.g. "H:\\Datascraper\\data\\ipindia\\...")
from before the data/ folder was migrated to the server — found 2026-09-22
while investigating why the Google Drive backfill script
(scripts/backfill_drive_migration.py) only found 22,576 of 30,420 real PDFs
on disk as candidates, and even then most of those "candidates" actually
pointed at a path that doesn't exist on this machine at all.

Root cause: source_pdf_path was written verbatim by the local scraper
process at fetch time (see pdf_scraper/pdf_processor.py's process_pdf) and
never rewritten when the data/ folder was copied to the server — the
column stores an absolute path, not a relative one, so a local-machine
path baked in at write time stays wrong forever until fixed once.

This script reconstructs the correct path directly from app_no +
source_pdf_filename using the same DATA_DIR/ipindia/<slugify(app_no)>/
<filename> convention every save/serve code path already uses (see
folder_export.py's DATA_DIR/slugify, pdf_scraper/pdf_processor.py's
process_pdf) — the exact same reconstruction backfill_drive_migration.py's
_resolve_local_path already does for ipindia_reminders/ipindia_correspondence,
applied here to tma_documents instead.

Only updates a row if the reconstructed path actually exists on disk as a
real file — never guesses, never touches a row it can't verify. Rows for
which no matching file can be found are left untouched and reported at
the end for manual review (could be genuinely missing files, not just a
bad path).

Usage (on the server, from /opt/tmtracker):
    sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/repair_tma_document_paths.py --dry-run
    sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/repair_tma_document_paths.py
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from folder_export import DATA_DIR, slugify  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, app_no, source_pdf_filename, source_pdf_path
            FROM tma_documents
            WHERE source_pdf_path IS NOT NULL
              AND source_pdf_path NOT LIKE %(prefix)s
            ORDER BY id
            """,
            {"prefix": f"{DATA_DIR}%"},
        ).fetchall()
    finally:
        conn.close()

    logging.info("%d row(s) with a path not under this machine's DATA_DIR (%s)", len(rows), DATA_DIR)

    fixed = missing = 0
    still_missing: list[tuple[int, str, str]] = []

    for row in rows:
        if not row["source_pdf_filename"]:
            missing += 1
            still_missing.append((row["id"], row["app_no"], "no source_pdf_filename recorded"))
            continue

        candidate = DATA_DIR / "ipindia" / slugify(row["app_no"]) / row["source_pdf_filename"]
        if not candidate.is_file():
            missing += 1
            still_missing.append((row["id"], row["app_no"], str(candidate)))
            continue

        if args.dry_run:
            fixed += 1
            continue

        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE tma_documents SET source_pdf_path = %(path)s WHERE id = %(id)s",
                {"path": str(candidate), "id": row["id"]},
            )
            conn.commit()
        finally:
            conn.close()
        fixed += 1

    logging.info(
        "%s%d row(s) %s, %d row(s) left unmatched (file not found)",
        "DRY RUN: would fix " if args.dry_run else "",
        fixed, "fixed" if not args.dry_run else "",
        missing,
    )

    if still_missing:
        print("\nRows left unmatched (manual review — file genuinely missing, or filename recorded incorrectly):")
        for id_, app_no, detail in still_missing[:50]:
            print(f"  id={id_} app_no={app_no}: {detail}")
        if len(still_missing) > 50:
            print(f"  ... and {len(still_missing) - 50} more")


if __name__ == "__main__":
    main()
