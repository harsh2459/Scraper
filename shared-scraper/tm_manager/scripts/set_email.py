"""
Change an existing account's email address (their login username).

  python scripts/set_email.py old@example.com new@example.com
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import db  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python scripts/set_email.py <current email> <new email>")
        sys.exit(1)
    old_email, new_email = sys.argv[1], sys.argv[2]

    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE lower(email) = lower(%s)", (old_email,)
        ).fetchone()
        if not row:
            print(f"No account found for {old_email!r}.")
            return

        taken = conn.execute(
            "SELECT id FROM users WHERE lower(email) = lower(%s) AND id != %s", (new_email, row["id"]),
        ).fetchone()
        if taken:
            print(f"{new_email!r} is already used by another account (id={taken['id']}). Nothing changed.")
            return

        conn.execute("UPDATE users SET email = %s WHERE id = %s", (new_email, row["id"]))
        conn.commit()
        print(f"Updated account id={row['id']}: {old_email!r} -> {new_email!r}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
