# Setup

## 1. Dependencies
Python 3.13+ (the OTP IMAP IDLE fast-path uses `imaplib.idle()`), Node.js for the frontends, PostgreSQL 14+.

```
python -m venv .venv && .venv\Scripts\activate
pip install -r shared-scraper/requirements.txt
pip install -r shared-scraper/tm_manager/requirements.txt
pip install -r shared-scraper/control_plane/requirements.txt   # fleet control plane only
pip install -r shared-scraper/worker_agent/requirements.txt    # fleet worker only
cd shared-scraper/tm_manager/frontend && npm install
```

## 2. Environment files (not in the repo - they hold secrets)
```
copy shared-scraper\.env.example shared-scraper\.env
copy shared-scraper\tm_manager\.env.example shared-scraper\tm_manager\.env
```
Fill in every value. Deployment-specific templates are in `deploy/*.env.example`.
Values that must match across files: `TM_MANAGER_OPS_USER/PASS` (root) = `OPS_API_USER/PASS` (tm_manager);
the `GOOGLE_DRIVE_*` values are the same in both.

## 3. External accounts / services you need
| Service | Used for | Env keys |
|---|---|---|
| PostgreSQL | main database | `PG_*` (optionally `PG_SSLCERT/PG_SSLKEY` for client-cert auth; password is the fallback) |
| Gmail (IMAP + app password) | reading portal login OTPs | `OTP_*`, `DIGEST_EMAIL_*` |
| Google Drive (OAuth) | document storage | `GOOGLE_DRIVE_*` - generate with `shared-scraper/scripts/google_drive_auth_setup.py` |
| Grok / xAI API key | captcha solving | `GROK_API_KEY` |
| Trademark portal accounts | the accounts being scraped | stored in the DB; see `deploy/import_legacy_portal_accounts.py` |
| Fleet coordinator + PDF scraper (optional) | worker mode | `FLEET_*`, `PDF_SCRAPER_*` |

## 4. Certificates (optional)
If using Postgres client-cert auth, put `ca.crt`, `client.crt`, `client.key` in `shared-scraper/certs/` (git-ignored).

## 5. Database
Create the database named in `PG_DBNAME`; tables are created by the app / `migrate_to_postgres.py`.
Data dumps are not in the repo.

## 6. Run
See `shared-scraper/README.md`, `deploy/README.md`, and `run-all.ps1`.
