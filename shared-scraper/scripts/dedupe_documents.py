"""
One-off cleanup: find and remove duplicate document PDFs already saved on
disk from before the status-unchanged skip + per-document skip fixes
existed (a re-fetch used to re-download and re-save every document under
a new -1/-2/-3 suffix even when nothing changed).

Only removes files that are byte-for-byte identical to another file for
the same app_no — never guesses based on filename alone. When duplicates
are found, the OLDEST file (by mtime) is kept; every newer identical copy
is deleted. If any surviving/deleted file's path is referenced by a
tma_documents row, that row's source_pdf_path/source_pdf_filename is
repointed to the kept file so nothing goes stale.

  python scripts/dedupe_documents.py                # just report, delete nothing
  python scripts/dedupe_documents.py --run           # actually delete + repoint
"""

import argparse
import hashlib
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import db  # noqa: E402
from folder_export import DATA_DIR  # noqa: E402


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_real_pdf(path: Path) -> bool:
    """False for a saved portal error/HTML page mistakenly given a .pdf
    extension (see ipindia_scraper.download_document's fix) — these are
    NOT duplicate documents even when several of them are byte-identical
    to each other, since they're all just copies of the same generic
    failure page for otherwise-different, never-actually-downloaded
    documents. Deleting all-but-one of these would destroy the distinct
    filenames of documents that still need to be re-fetched for real."""
    try:
        with open(path, "rb") as f:
            head = f.read(5)
        return head == b"%PDF-"
    except Exception:
        return False


def find_duplicate_groups() -> list[list[Path]]:
    """Returns a list of groups; each group is a list of Paths with
    identical content for the same app_no folder, oldest first. Only
    groups with more than one file are returned (real duplicates)."""
    ipindia_dir = DATA_DIR / "ipindia"
    groups: list[list[Path]] = []
    if not ipindia_dir.is_dir():
        return groups

    for app_folder in ipindia_dir.iterdir():
        if not app_folder.is_dir():
            continue
        pdfs = sorted(app_folder.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
        # Real PDFs only — a saved portal error/HTML page (see _is_real_pdf)
        # is never treated as a duplicate, even if several of them happen
        # to be byte-identical to each other; those are broken downloads
        # for otherwise-distinct documents, not real repeats.
        pdfs = [p for p in pdfs if _is_real_pdf(p)]
        if len(pdfs) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        for p in pdfs:
            try:
                by_hash[_hash_file(p)].append(p)
            except Exception:
                continue
        for paths in by_hash.values():
            if len(paths) > 1:
                groups.append(paths)  # already oldest-first from the sorted glob
    return groups


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()

    groups = find_duplicate_groups()
    if not groups:
        print("No duplicate documents found.")
        return

    total_to_delete = sum(len(g) - 1 for g in groups)
    print(f"Found {len(groups)} group(s) of duplicate documents, {total_to_delete} file(s) to remove:\n")

    conn = db.get_connection() if args.run else None
    try:
        for group in groups:
            keep, drop = group[0], group[1:]
            print(f"  KEEP: {keep}")
            for p in drop:
                print(f"  DROP: {p}")
            print()

            if not args.run:
                continue

            for p in drop:
                # Repoint any tma_documents row pointing at the file we're
                # about to delete, to the one we're keeping instead — avoids
                # a dangling source_pdf_path after deletion.
                str_p = str(p)
                str_keep = str(keep)
                conn.execute(
                    """
                    UPDATE tma_documents
                    SET source_pdf_path = %(keep)s, source_pdf_filename = %(keep_name)s
                    WHERE source_pdf_path = %(drop)s
                    """,
                    {"keep": str_keep, "keep_name": keep.name, "drop": str_p},
                )
                try:
                    p.unlink()
                except Exception as exc:
                    print(f"    Could not delete {p}: {exc}")
            conn.commit()

        if args.run:
            print(f"Deleted {total_to_delete} duplicate file(s).")
        else:
            print("Re-run with --run to actually delete these and repoint any database references.")
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
