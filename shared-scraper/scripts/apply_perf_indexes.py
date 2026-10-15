"""
Apply scripts/perf_indexes.sql to the datascraper DB.

Opens ONE autocommit psycopg connection (CREATE INDEX CONCURRENTLY cannot run
inside a transaction), runs each statement, prints OK/skip/error, then ANALYZEs
the touched tables. Idempotent — safe to re-run.

  python scripts/apply_perf_indexes.py
"""

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import psycopg  # noqa: E402
import config  # noqa: E402

SQL_FILE = _ROOT / "scripts" / "perf_indexes.sql"
ANALYZE_TABLES = ["ipindia_results", "tma_documents", "trademarks", "my_trademarks", "tm_leads", "work_items"]


def _split_statements(sql_text: str) -> list[str]:
    # strip -- comments, then split on ';'
    lines = []
    for line in sql_text.splitlines():
        s = line.split("--", 1)[0]
        lines.append(s)
    joined = "\n".join(lines)
    return [s.strip() for s in joined.split(";") if s.strip()]


def main() -> int:
    conninfo = (
        f"host={config.PG_HOST} port={config.PG_PORT} dbname={config.PG_DBNAME} "
        f"user={config.PG_USER}"
    )
    if config.PG_PASSWORD:
        conninfo += f" password={config.PG_PASSWORD}"

    stmts = _split_statements(SQL_FILE.read_text(encoding="utf-8"))
    print(f"{len(stmts)} statement(s) from {SQL_FILE.name}\n")

    errors = 0
    with psycopg.connect(conninfo, autocommit=True) as conn:
        for i, stmt in enumerate(stmts, 1):
            label = re.sub(r"\s+", " ", stmt)[:90]
            try:
                conn.execute(stmt)
                print(f"  [{i:>2}/{len(stmts)}] OK    {label}")
            except psycopg.errors.DuplicateTable:
                print(f"  [{i:>2}/{len(stmts)}] skip  (exists) {label}")
            except Exception as exc:
                errors += 1
                print(f"  [{i:>2}/{len(stmts)}] ERROR {label}\n           -> {exc}")

        print("\nANALYZE:")
        for t in ANALYZE_TABLES:
            try:
                conn.execute(f"ANALYZE {t}")
                print(f"  ANALYZE {t}  OK")
            except Exception as exc:
                print(f"  ANALYZE {t}  ERROR -> {exc}")

        # report any INVALID indexes left by an interrupted CONCURRENTLY build
        invalid = conn.execute(
            """
            SELECT c.relname
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            WHERE NOT i.indisvalid AND c.relname LIKE 'idx_%'
            """
        ).fetchall()
        if invalid:
            print("\n!! INVALID indexes (DROP INDEX <name>; then re-run):")
            for row in invalid:
                print(f"   {row[0]}")

    print(f"\nDone. {errors} error(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
