"""
Find reminders/correspondence rows that SHOULD have a downloadable document
(the portal gave us a corres_num / view_url) but don't have one saved
(document_path IS NULL) — a genuine download failure, not a case where the
portal simply never had a file for that notice — and re-fetch those
app_nos through the normal live-search path so the download gets retried.

Rows with no corres_num/view_url at all are never included here: that
means the portal itself has no attached document for that notice, which
is expected and not a failure.

Safe to re-run: /api/scrape-appno-batch re-fetches everything for an
app_no (status, documents, reminders, correspondence) and the DB's own
COALESCE-on-save logic never clobbers a document_path that's already set
— only fills in ones still missing.

  python scripts/retry_missing_correspondence_docs.py            # just list them
  python scripts/retry_missing_correspondence_docs.py --run      # actually re-fetch
"""

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
import db  # noqa: E402
import requests  # noqa: E402


def find_affected_app_nos(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT app_no FROM ipindia_reminders
        WHERE corres_num IS NOT NULL AND corres_num != '' AND document_path IS NULL
        UNION
        SELECT DISTINCT app_no FROM ipindia_correspondence
        WHERE view_url IS NOT NULL AND view_url != '' AND document_path IS NULL
        """
    ).fetchall()
    return sorted({r["app_no"] for r in rows})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="Actually re-fetch (default: just list).")
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        app_nos = find_affected_app_nos(conn)
    finally:
        conn.close()

    print(f"{len(app_nos)} application number(s) have a reminder/correspondence document that failed to download.")
    for a in app_nos:
        print(f"  {a}")

    if not app_nos or not args.run:
        if app_nos and not args.run:
            print("\nRe-run with --run to actually re-fetch these.")
        return

    auth = (config.BASIC_AUTH_USER, config.BASIC_AUTH_PASS) if config.BASIC_AUTH_USER else None
    base = "http://127.0.0.1:5001"

    for i in range(0, len(app_nos), args.batch_size):
        batch = app_nos[i:i + args.batch_size]
        print(f"\nSubmitting batch of {len(batch)}: {batch}")
        resp = requests.post(
            f"{base}/api/scrape-appno-batch",
            json={"app_nos": batch, "requested_by": "retry_missing_correspondence_docs"},
            auth=auth, timeout=30,
        )
        if resp.status_code != 202:
            print(f"  Could not start batch: HTTP {resp.status_code} {resp.text[:200]}")
            continue
        job_id = resp.json().get("job_id")
        print(f"  job_id={job_id} — waiting for it to finish…")
        while True:
            time.sleep(5)
            status = requests.get(f"{base}/api/scrape-appno-batch/{job_id}", auth=auth, timeout=15).json()
            if status.get("status") == "done":
                print(f"  Done: {status.get('results')}")
                break
            if status.get("status") == "unknown":
                print("  Job lost (scraper restarted?) — skipping rest of this batch.")
                break


if __name__ == "__main__":
    main()
