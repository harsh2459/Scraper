"""
Multi-tenant migration, stage 2: create the one admin account and backfill
every existing my_trademarks row to it.

Safe to re-run: creating the same email twice just reuses the existing
account (no duplicate), and the backfill only ever touches rows where
user_id IS NULL, so running this again after new users/trademarks exist
does nothing to their data.

  python scripts/create_admin_and_backfill.py you@example.com "a real password"
  python scripts/create_admin_and_backfill.py you@example.com "a real password" --dry-run
"""

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import db  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("email")
    parser.add_argument("password")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        existing = conn.execute(
            "SELECT id, is_admin FROM users WHERE lower(email) = lower(%s)", (args.email,)
        ).fetchone()

        if existing:
            admin_id = existing["id"]
            print(f"User {args.email!r} already exists (id={admin_id}).")
            if not existing["is_admin"]:
                if args.dry_run:
                    print("  Would set is_admin=true (dry run).")
                else:
                    conn.execute("UPDATE users SET is_admin = true WHERE id = %s", (admin_id,))
                    conn.commit()
                    print("  Set is_admin=true.")
        else:
            if args.dry_run:
                print(f"Would create admin user {args.email!r} (dry run).")
                admin_id = None
            else:
                password_hash = generate_password_hash(args.password)
                row = conn.execute(
                    """
                    INSERT INTO users (email, password_hash, is_admin)
                    VALUES (%s, %s, true)
                    RETURNING id
                    """,
                    (args.email, password_hash),
                ).fetchone()
                conn.commit()
                admin_id = row["id"]
                print(f"Created admin user {args.email!r} (id={admin_id}).")

        pending = conn.execute(
            "SELECT count(*) AS n FROM my_trademarks WHERE user_id IS NULL"
        ).fetchone()["n"]
        print(f"my_trademarks rows with no owner yet: {pending}")

        if pending and admin_id is not None:
            if args.dry_run:
                print(f"  Would assign all {pending} row(s) to user_id={admin_id} (dry run).")
            else:
                cursor = conn.execute(
                    "UPDATE my_trademarks SET user_id = %s WHERE user_id IS NULL", (admin_id,)
                )
                conn.commit()
                print(f"  Assigned {cursor.rowcount} row(s) to user_id={admin_id}.")

        remaining = conn.execute(
            "SELECT count(*) AS n FROM my_trademarks WHERE user_id IS NULL"
        ).fetchone()["n"]
        print(f"my_trademarks rows still with no owner: {remaining}")
        if remaining == 0:
            if args.dry_run:
                print("Would lock user_id to NOT NULL and swap the unique constraint now (dry run).")
            else:
                db.lock_my_trademarks_to_users(conn)
                print("Locked: user_id is now required, and the unique constraint is (user_id, app_no).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
