"""
One-time cleanup: removes duplicate tma_documents rows created by the
local-disk-only collision check bug (see pdf_processor.py's
find_tma_document_by_base_name fix) — before that fix, a document already
saved on Google Drive (no local file) looked "new" on every re-fetch of
the same app_no, so it got re-downloaded and re-inserted under a -1/-2/...
suffix on every staleness refresh or retry.

For each (app_no, base document name) group — where "tm-a-trade-marks-
application.pdf", "tm-a-trade-marks-application-1.pdf", "...-2.pdf" etc.
all count as the same logical document — this keeps the OLDEST row (the
real original, since duplicates were always created strictly later) and
deletes the rest.

Only removes DATABASE ROWS and any local disk files under this row's own
source_pdf_path/logo_path — it does NOT delete anything from Google
Drive (DriveClient has no delete method, and deleting real uploaded files
is not something to do without a human looking first). Duplicate Drive
files are simply left orphaned/unreferenced; clean those up separately
and manually if you want the storage back.

Usage:
    python scripts/dedupe_tma_documents.py            # dry run — reports only, deletes nothing
    python scripts/dedupe_tma_documents.py --apply     # actually deletes the duplicate rows/local files
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

_ROOT_DIR = Path(__file__).parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import config  # noqa: E402
import db  # noqa: E402

_SUFFIX_RE = re.compile(r"^(?P<stem>.+?)(?:-(?P<n>\d+))?\.(?P<ext>[^.]+)$")


def _base_key(filename: str) -> str:
    """Collapses 'name.pdf', 'name-1.pdf', 'name-2.pdf' to the same key
    ('name.pdf') so they group together as one logical document."""
    m = _SUFFIX_RE.match(filename)
    if not m:
        return filename
    return f"{m.group('stem')}.{m.group('ext')}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually delete duplicates (default: dry run)")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, app_no, source_pdf_filename, source_pdf_path, drive_file_id,
                   logo_path, logo_drive_file_id, extracted_at
            FROM tma_documents
            WHERE source_pdf_filename IS NOT NULL
            ORDER BY app_no, extracted_at ASC
            """
        ).fetchall()

        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in rows:
            key = (row["app_no"], _base_key(row["source_pdf_filename"]))
            groups[key].append(row)

        duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}
        total_to_delete = sum(len(v) - 1 for v in duplicate_groups.values())

        print(f"Scanned {len(rows)} tma_documents rows across {len(groups)} logical documents.")
        print(f"Found {len(duplicate_groups)} document(s) with duplicates, {total_to_delete} duplicate row(s) to remove.\n")

        if not duplicate_groups:
            print("Nothing to clean up.")
            return

        for (app_no, base_name), group in duplicate_groups.items():
            keep, dupes = group[0], group[1:]
            print(f"{app_no} / {base_name}: keeping id={keep['id']} ({keep['extracted_at']}), removing {len(dupes)} duplicate(s)")
            for dupe in dupes:
                drive_note = f", orphaning Drive file {dupe['drive_file_id']}" if dupe["drive_file_id"] else ""
                print(f"    - id={dupe['id']} filename={dupe['source_pdf_filename']!r} extracted_at={dupe['extracted_at']}{drive_note}")
                if not args.apply:
                    continue
                if dupe["source_pdf_path"]:
                    try:
                        Path(dupe["source_pdf_path"]).unlink(missing_ok=True)
                    except Exception as exc:
                        print(f"      (could not delete local file {dupe['source_pdf_path']}: {exc})")
                if dupe["logo_path"]:
                    try:
                        Path(dupe["logo_path"]).unlink(missing_ok=True)
                    except Exception as exc:
                        print(f"      (could not delete local logo {dupe['logo_path']}: {exc})")
                conn.execute("DELETE FROM tma_documents WHERE id = %(id)s", {"id": dupe["id"]})

        if args.apply:
            conn.commit()
            print(f"\nDeleted {total_to_delete} duplicate row(s).")
        else:
            print(f"\nDry run only — nothing deleted. Re-run with --apply to actually remove these {total_to_delete} row(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
