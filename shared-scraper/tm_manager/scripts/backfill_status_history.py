"""
Backfill tm_status_history from the ipindia_results rows already on disk.

For every my_trademarks row, walk its appno scrapes in chronological order and
insert one tm_status_history snapshot per scrape, computing the per-field diff
against the previous one — exactly as mark_trademark_fetched now does going
forward. Idempotent: the unique (my_trademark_id, search_id) index means
re-running skips rows already present.

  python scripts/backfill_status_history.py            # all marks
  python scripts/backfill_status_history.py --tm 121   # one mark
  python scripts/backfill_status_history.py --dry-run
"""

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import db  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402


def _scrapes_for(conn, app_no: str) -> list[dict]:
    return conn.execute(
        """
        SELECT id, data_json, scraped_at
        FROM ipindia_results
        WHERE query = %s
          AND search_type IN ('appno', 'appno_discover')
          AND data_json IS NOT NULL
          AND jsonb_array_length(data_json) > 0
          AND (error IS NULL OR error = '')
        ORDER BY scraped_at ASC, id ASC
        """,
        (app_no,),
    ).fetchall()


def _snap(payload: dict) -> dict:
    return {col: (payload.get(label) or None) for label, col in db._TSH_FIELDS.items()}


def backfill(dry_run: bool = False, only_tm: int | None = None) -> None:
    conn = db.get_connection()
    try:
        marks = conn.execute(
            "SELECT id, app_no FROM my_trademarks"
            + (" WHERE id = %s" if only_tm else "")
            + " ORDER BY id",
            (only_tm,) if only_tm else (),
        ).fetchall()

        total_inserted = 0
        for m in marks:
            existing = {
                r["search_id"]
                for r in conn.execute(
                    "SELECT search_id FROM tm_status_history WHERE my_trademark_id = %s",
                    (m["id"],),
                ).fetchall()
                if r["search_id"] is not None
            }
            scrapes = _scrapes_for(conn, m["app_no"])
            prev_snap: dict | None = None
            # seed prev_snap from the latest already-recorded row, if any
            latest = conn.execute(
                """
                SELECT status, sub_status, trade_mark, proprietor_name, class_number,
                       valid_upto, publication_details, tm_type
                FROM tm_status_history WHERE my_trademark_id = %s
                ORDER BY fetched_at DESC, id DESC LIMIT 1
                """,
                (m["id"],),
            ).fetchone()
            if latest:
                prev_snap = dict(latest)

            inserted_here = 0
            for sc in scrapes:
                payload = (sc["data_json"][0] if sc["data_json"] else {}) or {}
                snap = _snap(payload)
                is_first = prev_snap is None
                changed: dict = {}
                if not is_first:
                    for col in snap:
                        if (prev_snap.get(col) or None) != (snap.get(col) or None):
                            changed[col] = {"from": prev_snap.get(col), "to": snap.get(col)}
                if sc["id"] not in existing:
                    if not dry_run:
                        conn.execute(
                            """
                            INSERT INTO tm_status_history
                                (my_trademark_id, app_no, search_id, fetched_at,
                                 status, sub_status, trade_mark, proprietor_name, class_number,
                                 valid_upto, publication_details, tm_type, changed_fields, is_first)
                            VALUES
                                (%(tm)s, %(app_no)s, %(sid)s, %(fa)s,
                                 %(status)s, %(sub_status)s, %(trade_mark)s, %(proprietor_name)s, %(class_number)s,
                                 %(valid_upto)s, %(publication_details)s, %(tm_type)s, %(changed)s, %(is_first)s)
                            ON CONFLICT (my_trademark_id, search_id) WHERE search_id IS NOT NULL DO NOTHING
                            """,
                            {
                                "tm": m["id"], "app_no": m["app_no"], "sid": sc["id"],
                                "fa": sc["scraped_at"], "changed": Jsonb(changed),
                                "is_first": is_first, **snap,
                            },
                        )
                    inserted_here += 1
                prev_snap = snap

            if inserted_here:
                if not dry_run:
                    conn.commit()
                total_inserted += inserted_here
                print(f"  tm {m['id']:>4} ({m['app_no']}): +{inserted_here} snapshot(s) from {len(scrapes)} scrape(s)")

        print(f"\n{'(dry run) would insert' if dry_run else 'inserted'} {total_inserted} history row(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--tm", type=int, help="only this my_trademarks id")
    args = p.parse_args()
    backfill(dry_run=args.dry_run, only_tm=args.tm)
