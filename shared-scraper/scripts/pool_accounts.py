"""
Manage IP India OTP pool accounts and their split between the two pools:

  warm  -> reserved_for = 'warm_pool'   fast lane: kept logged in at startup,
                                        used for instant appno fetches (no OTP wait)
  cold  -> reserved_for = NULL          regular scraper: claimed on demand, full
                                        login each time, released after use

Usage
-----
  python scripts/pool_accounts.py add   <email> <app_pw> --role warm|cold [--imap imap.gmail.com] [--port 993] [--no-test]
  python scripts/pool_accounts.py set-role <email> warm|cold
  python scripts/pool_accounts.py list
  python scripts/pool_accounts.py enable  <email>
  python scripts/pool_accounts.py disable <email>
  python scripts/pool_accounts.py remove  <email>
  python scripts/pool_accounts.py test    <email>

`add` runs a live IMAP login test and stores the result on the row unless
--no-test is given. Run from the h:/Datascraper directory (or anywhere — it
adds the repo root to sys.path).
"""

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
import db  # noqa: E402
from email_otp import EmailOTPReader  # noqa: E402


def _imap_test(email: str, app_pw: str, imap_server: str, imap_port: int) -> tuple[bool, str]:
    reader = EmailOTPReader(email, app_pw, imap_server, imap_port)
    try:
        if reader.connect():
            reader.disconnect()
            return True, "IMAP login OK"
        return False, "IMAP login failed (bad app-password or IMAP not enabled)"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"IMAP error: {exc}"


def _print_row(r: dict, prefix: str = "  ") -> None:
    role = "warm" if r["reserved_for"] == "warm_pool" else "cold"
    tested = r.get("last_test_ok")
    tested_s = "test=OK" if tested else ("test=FAIL" if tested is False else "test=?")
    en = "enabled" if r["enabled"] else "DISABLED"
    print(f"{prefix}[{r['id']:>3}] {r['email_address']:<32} {role:<4} {en:<9} {tested_s}")


def cmd_list(conn) -> int:
    grouped = db.list_pool_accounts_by_role(conn)
    print(f"WARM  (fast lane, kept logged in) — {len(grouped['warm'])} account(s):")
    for r in grouped["warm"]:
        _print_row(r)
    if not grouped["warm"]:
        print("  (none — all fetches use the cold login path)")
    print(f"\nCOLD  (regular scraper, login-per-fetch) — {len(grouped['cold'])} account(s):")
    for r in grouped["cold"]:
        _print_row(r)
    if not grouped["cold"]:
        print("  (none)")
    return 0


def cmd_add(conn, args) -> int:
    ok, msg = (True, "skipped")
    if not args.no_test:
        print(f"Testing IMAP login for {args.email} …")
        ok, msg = _imap_test(args.email, args.app_pw, args.imap, args.port)
        print(f"  IMAP test: {'OK' if ok else 'FAILED'} — {msg}")
        if not ok:
            print("  Not adding. Fix the app-password / enable IMAP, or pass --no-test to add anyway.")
            return 1
    row = db.upsert_pool_account(
        conn, args.email, args.app_pw, args.imap, args.port, role=args.role
    )
    if not args.no_test:
        db.record_pool_test_result(conn, row["id"], ok, msg)
    print(f"Added / updated:")
    _print_row(db.get_pool_account_by_email(conn, args.email))
    if args.role == "warm":
        print("  -> restart the scraper (run-all.ps1) so the warm pool logs it in,")
        print("     or POST /api/pool/accounts/<email>/role {\"role\":\"warm\"} for a live add.")
    return 0


def cmd_set_role(conn, args) -> int:
    row = db.set_pool_account_role(conn, args.email, args.role)
    if row is None:
        print(f"No pool account with email {args.email!r}.")
        return 1
    _print_row(row)
    print(f"  role set to '{args.role}'. Restart the scraper (or use the /api/pool endpoint) to apply live.")
    return 0


def cmd_enable(conn, args) -> int:
    row = db.set_pool_account_enabled_by_email(conn, args.email, True)
    if row is None:
        print(f"No pool account with email {args.email!r}.")
        return 1
    _print_row(row)
    return 0


def cmd_disable(conn, args) -> int:
    row = db.set_pool_account_enabled_by_email(conn, args.email, False)
    if row is None:
        print(f"No pool account with email {args.email!r}.")
        return 1
    _print_row(row)
    return 0


def cmd_remove(conn, args) -> int:
    if db.remove_pool_account_by_email(conn, args.email):
        print(f"Removed {args.email}.")
        return 0
    print(f"No pool account with email {args.email!r}.")
    return 1


def cmd_test(conn, args) -> int:
    row = db.get_pool_account_by_email(conn, args.email)
    if row is None:
        print(f"No pool account with email {args.email!r}.")
        return 1
    ok, msg = _imap_test(row["email_address"], row["app_password"], row["imap_server"], row["imap_port"])
    db.record_pool_test_result(conn, row["id"], ok, msg)
    print(f"{args.email}: {'OK' if ok else 'FAILED'} — {msg}")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add or update an account")
    a.add_argument("email")
    a.add_argument("app_pw")
    a.add_argument("--role", choices=("warm", "cold"), default="cold")
    a.add_argument("--imap", default="imap.gmail.com")
    a.add_argument("--port", type=int, default=993)
    a.add_argument("--no-test", action="store_true", help="skip the live IMAP login test")

    sr = sub.add_parser("set-role", help="move an account between warm and cold")
    sr.add_argument("email")
    sr.add_argument("role", choices=("warm", "cold"))

    sub.add_parser("list", help="show the current warm/cold split")

    for name in ("enable", "disable", "remove", "test"):
        sp = sub.add_parser(name)
        sp.add_argument("email")

    args = p.parse_args(argv)

    conn = db.get_connection()
    try:
        if args.cmd == "add":
            return cmd_add(conn, args)
        if args.cmd == "set-role":
            return cmd_set_role(conn, args)
        if args.cmd == "list":
            return cmd_list(conn)
        if args.cmd == "enable":
            return cmd_enable(conn, args)
        if args.cmd == "disable":
            return cmd_disable(conn, args)
        if args.cmd == "remove":
            return cmd_remove(conn, args)
        if args.cmd == "test":
            return cmd_test(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
