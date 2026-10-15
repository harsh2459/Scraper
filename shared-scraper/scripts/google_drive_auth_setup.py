"""
One-time OAuth2 consent flow for this app's Google Drive integration.

Run this ONCE, locally (never on the server — it needs to open a real
browser window for you to log into your personal Google account and grant
access). It prints a refresh token to your terminal; paste that, along
with the client id/secret you got from Google Cloud Console, into BOTH
.env files this codebase reads (H:\\Datascraper\\.env and
H:\\Datascraper\\tm_manager\\.env — they're two separate config surfaces
with no shared secrets today, so both need the same three values). Never
write the refresh token to a file yourself — treat it exactly like any
other hand-entered credential already in this codebase (e.g.
DIGEST_EMAIL_APP_PASSWORD).

Usage:
    python scripts/google_drive_auth_setup.py <client_id> <client_secret>

Prerequisites (see deploy docs / the plan doc for full click-by-click
steps):
  1. A Google Cloud project with the Google Drive API enabled.
  2. An OAuth consent screen configured (type "External", your own Gmail
     added as a test user, publishing status "Testing" is fine).
  3. An OAuth Client ID of type "Desktop app" — use its Client ID and
     Client Secret as the two arguments to this script.

Scope requested: drive.file only — this app will only ever be able to
see/manage files IT creates through this integration, never browse or
touch anything else already in your Drive. Least-privilege by design.
"""

import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: python {sys.argv[0]} <client_id> <client_secret>")
        sys.exit(1)

    client_id, client_secret = sys.argv[1], sys.argv[2]

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    print("Opening your browser — log into the Google account you want this app to use,")
    print("and grant access when prompted...")
    credentials = flow.run_local_server(port=0)

    print()
    print("Success. Add these three lines to BOTH .env files")
    print("(H:\\Datascraper\\.env AND H:\\Datascraper\\tm_manager\\.env):")
    print()
    print(f"GOOGLE_DRIVE_CLIENT_ID={client_id}")
    print(f"GOOGLE_DRIVE_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_DRIVE_REFRESH_TOKEN={credentials.refresh_token}")
    print()
    print("Leave GOOGLE_DRIVE_ROOT_FOLDER_ID blank for now — a root folder")
    print("will be created automatically on first use.")


if __name__ == "__main__":
    main()
