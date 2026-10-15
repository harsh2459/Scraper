"""
One-time, idempotent helper that writes the GOOGLE_DRIVE_* credential
lines into both .env files on the server.

.env files are deliberately excluded from every "push everything" deploy
tarball (see deploy/HOW-TO-UPDATE.md) so a redeploy never clobbers
server-only secrets (real PG_PASSWORD, BASIC_AUTH_PASS, etc). That means
these four credential lines — obtained locally via
scripts/google_drive_auth_setup.py against the developer's own Google
account — have to be applied on the server as one short, explicit step,
same as any other server-only secret has always been added by hand.

This script only ever ADDS the four GOOGLE_DRIVE_* keys. It never removes
or rewrites any other line in either file, so it's safe to run even if
the .env already has unrelated content. Running it twice is a no-op the
second time (each key is only appended if not already present).

Usage (on the server, from /opt/tmtracker, ONE TIME after this code is
first deployed):
    sudo -u tmtracker /opt/tmtracker/.venv/bin/python scripts/apply_google_drive_env.py \\
        --client-id "..." --client-secret "..." --refresh-token "..." [--root-folder-id "..."]

Then restart all three services as usual:
    sudo systemctl restart tmtracker-scraper tmtracker-pdf-scraper tmtracker-backend

The credential values themselves are never stored in this repo or in the
deploy tarball — they're passed as command-line arguments each time this
is run, exactly like scripts/pool_accounts.py takes an app password as an
argument rather than reading it from a checked-in file.
"""

import argparse
from pathlib import Path

_ENV_FILES = [
    Path(__file__).resolve().parent.parent / ".env",
    Path(__file__).resolve().parent.parent / "tm_manager" / ".env",
]


def _upsert_lines(env_path: Path, values: dict[str, str]) -> None:
    if not env_path.is_file():
        print(f"  SKIP {env_path} (file does not exist — create it first)")
        return

    text = env_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    existing_keys = {line.split("=", 1)[0] for line in lines if "=" in line and not line.strip().startswith("#")}

    added = []
    for key, value in values.items():
        if key in existing_keys:
            print(f"  {env_path.name}: {key} already present — leaving it untouched")
            continue
        added.append(f"{key}={value}")

    if not added:
        print(f"  {env_path}: nothing to add")
        return

    if lines and lines[-1].strip():
        lines.append("")
    lines.append("# Google Drive document storage (added by apply_google_drive_env.py)")
    lines.extend(added)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  {env_path}: added {len(added)} line(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--refresh-token", required=True)
    parser.add_argument("--root-folder-id", default="")
    args = parser.parse_args()

    values = {
        "GOOGLE_DRIVE_CLIENT_ID": args.client_id,
        "GOOGLE_DRIVE_CLIENT_SECRET": args.client_secret,
        "GOOGLE_DRIVE_REFRESH_TOKEN": args.refresh_token,
        "GOOGLE_DRIVE_ROOT_FOLDER_ID": args.root_folder_id,
    }

    for env_path in _ENV_FILES:
        _upsert_lines(env_path, values)

    print("\nDone. Restart all three services for this to take effect:")
    print("  sudo systemctl restart tmtracker-scraper tmtracker-pdf-scraper tmtracker-backend")


if __name__ == "__main__":
    main()
