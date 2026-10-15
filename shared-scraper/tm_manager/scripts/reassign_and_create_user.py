"""
One-off fixup: move every trademark from one account to another, and
optionally create the destination account if it doesn't exist yet.

Use this when the migration's backfill accidentally landed all
pre-existing trademarks on the wrong account (e.g. a test account that
happened to be the first one created) and you want them moved to the real
owner's account instead.

  python scripts/reassign_and_create_user.py \
      --from harshbabariya2459@gmail.com \
      --to nvgalani96@gmail.com --to-password "a real password" --to-admin

  python scripts/reassign_and_create_user.py --from ... --to ... --to-password ... --dry-run
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
    parser.add_argument("--from", dest="from_email", required=True)
    parser.add_argument("--to", dest="to_email", required=True)
    parser.add_argument("--to-password", dest="to_password", help="Required if --to doesn't already exist.")
    parser.add_argument("--to-admin", action="store_true", help="Make the destination account an admin.")
    parser.add_argument("--demote-from", action="store_true", help="Also strip admin from the source account.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        from_user = conn.execute(
            "SELECT id, email, is_admin FROM users WHERE lower(email) = lower(%s)", (args.from_email,)
        ).fetchone()
        if not from_user:
            print(f"No account found for --from {args.from_email!r}. Nothing to do.")
            return

        to_user = conn.execute(
            "SELECT id, email, is_admin FROM users WHERE lower(email) = lower(%s)", (args.to_email,)
        ).fetchone()
        if not to_user:
            if not args.to_password:
                print(f"--to {args.to_email!r} doesn't exist yet — pass --to-password to create it.")
                return
            if args.dry_run:
                print(f"Would create account {args.to_email!r} (admin={args.to_admin}).")
                to_id = None
            else:
                row = conn.execute(
                    "INSERT INTO users (email, password_hash, is_admin) VALUES (%s, %s, %s) RETURNING id",
                    (args.to_email, generate_password_hash(args.to_password), args.to_admin),
                ).fetchone()
                conn.commit()
                to_id = row["id"]
                print(f"Created account {args.to_email!r} (id={to_id}, admin={args.to_admin}).")
        else:
            to_id = to_user["id"]
            print(f"Account {args.to_email!r} already exists (id={to_id}).")
            if args.to_admin and not to_user["is_admin"]:
                if args.dry_run:
                    print("  Would set is_admin=true (dry run).")
                else:
                    conn.execute("UPDATE users SET is_admin = true WHERE id = %s", (to_id,))
                    conn.commit()
                    print("  Set is_admin=true.")

        count = conn.execute(
            "SELECT count(*) AS n FROM my_trademarks WHERE user_id = %s", (from_user["id"],)
        ).fetchone()["n"]
        print(f"{args.from_email!r} currently owns {count} trademark(s).")

        if count and to_id is not None:
            if args.dry_run:
                print(f"  Would move all {count} to {args.to_email!r} (id={to_id}) (dry run).")
            else:
                # ON CONFLICT can only happen if the destination account
                # already independently tracks the same app_no — extremely
                # unlikely for a fresh account, but handled safely by simply
                # leaving that one row where it is rather than erroring the
                # whole batch out.
                moved = conn.execute(
                    """
                    UPDATE my_trademarks
                    SET user_id = %(to_id)s, updated_at = now()
                    WHERE user_id = %(from_id)s
                      AND NOT EXISTS (
                          SELECT 1 FROM my_trademarks t2
                          WHERE t2.user_id = %(to_id)s AND t2.app_no = my_trademarks.app_no
                      )
                    """,
                    {"to_id": to_id, "from_id": from_user["id"]},
                ).rowcount
                conn.commit()
                print(f"  Moved {moved} trademark(s) to {args.to_email!r}.")
                skipped = count - moved
                if skipped:
                    print(f"  Skipped {skipped} row(s) that {args.to_email!r} already tracks independently.")

        if args.demote_from and from_user["is_admin"]:
            if args.dry_run:
                print(f"Would strip admin from {args.from_email!r} (dry run).")
            else:
                conn.execute("UPDATE users SET is_admin = false WHERE id = %s", (from_user["id"],))
                conn.commit()
                print(f"Stripped admin from {args.from_email!r}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
