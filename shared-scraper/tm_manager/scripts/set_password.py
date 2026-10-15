"""
Reset an existing account's password (there's no self-service "change
password" UI yet — use this until one exists).

  python scripts/set_password.py someone@example.com "new password"
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import db  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python scripts/set_password.py <email> <new password>")
        sys.exit(1)
    email, password = sys.argv[1], sys.argv[2]

    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE lower(email) = lower(%s)", (email,)
        ).fetchone()
        if not row:
            print(f"No account found for {email!r}.")
            return
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (generate_password_hash(password), row["id"]),
        )
        conn.commit()
        print(f"Password updated for {email!r} (id={row['id']}).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
