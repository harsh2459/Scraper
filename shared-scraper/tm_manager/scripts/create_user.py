"""
Create a new TM Manager account — regular user by default, or admin with
--admin. Signup is invite-only (no public signup page), so this script is
how you add every new person.

  python scripts/create_user.py newperson@example.com "their password"
  python scripts/create_user.py newperson@example.com "their password" --admin
  python scripts/create_user.py newperson@example.com "their password" --name "Jane Doe"
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
    parser.add_argument("--admin", action="store_true", help="Give this account admin (sees every user's trademarks).")
    parser.add_argument("--name", dest="display_name", default=None)
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM users WHERE lower(email) = lower(%s)", (args.email,)
        ).fetchone()
        if existing:
            print(f"An account for {args.email!r} already exists (id={existing['id']}). Nothing created.")
            return

        row = conn.execute(
            """
            INSERT INTO users (email, password_hash, is_admin, display_name)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (args.email, generate_password_hash(args.password), args.admin, args.display_name),
        ).fetchone()
        conn.commit()
        print(f"Created {'admin' if args.admin else 'regular'} account {args.email!r} (id={row['id']}). They start with an empty portfolio.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
