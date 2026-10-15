# Restore access to existing Google Drive documents

The user confirmed that the failing saved document opens with `chatgptbmaemp369@gmail.com`. The local original and updated configurations currently authenticate as `harshbabariya2459@gmail.com`. Contabo's connected account has not yet been independently identified. Its sample metadata request returned HTTP 404; a wrong account or missing app access remains a possible cause, rather than proof the file was deleted.

`drive_account_repair.py` provides local browser authorization and an explicit Linux-server apply step. It never prints tokens, changes database IDs, uploads documents, removes documents or restarts services. It retains the existing `drive.file` permission. Signing into the correct account does not by itself prove that the OAuth app can read every older document.

## Authorize on the Windows PC

From this workspace in PowerShell:

```powershell
cd H:\Datascraper-updates
python deploy\drive_account_repair.py authorize --root shared-scraper --account chatgptbmaemp369@gmail.com --output .runtime\drive-auth\authorization.json
```

Select the indicated account in Google's browser sign-in and approve the requested app access. The helper verifies the account with Google before writing a private authorization bundle. The bundle directory has protected Windows access rules for the current owner, SYSTEM and Administrators, with owner-only rights accepted only when the directory belongs to the current user. A wrong account or failed permission check does not export credentials. The local application `.env` files are unchanged.

If Google rejects the account as a test user, add that account to the existing OAuth project's test-user list and retry. Do not create a different OAuth client solely to bypass that message. Do not print or paste the private JSON into chat. The helper refuses to overwrite an existing export; a subsequent authorization requires a different output filename.

## Transfer privately and apply on Contabo

These commands are for the user to run with their existing server access; the coding agent has not accessed the server. On the PC:

```powershell
ssh root@62.146.239.158 "umask 077; mkdir -p /root/.tmtracker-drive; chmod 700 /root/.tmtracker-drive"
scp deploy\drive_account_repair.py .runtime\drive-auth\authorization.json root@62.146.239.158:/root/.tmtracker-drive/
```

The server directory is private before the credentials are copied. In the server terminal:

```bash
sudo /opt/tmtracker/.venv/bin/python /root/.tmtracker-drive/drive_account_repair.py apply \
  --root /opt/tmtracker \
  --account chatgptbmaemp369@gmail.com \
  --credentials /root/.tmtracker-drive/authorization.json

sudo systemctl restart tmtracker-scraper tmtracker-pdf-scraper tmtracker-backend
```

Apply refreshes the supplied authorization and verifies its account before replacing the three OAuth fields in both existing application `.env` files. It creates private backups, removes duplicate occurrences of those three keys, preserves other settings and existing root-folder IDs, and retains each config file's owner with owner-only permissions. Unlike the older append-only environment helper, it actually replaces already-present authorization fields. An apply error can require restoring from its private backups; inspect the reported status before restarting services.

## Verify before local cleanup

Repeat the existing read-only metadata check on the previously failing saved file ID. Confirm that metadata is accessible and `md5Checksum` is available, then open the document through the application. Verify that both app processes use the corrected authorization, including any systemd environment overrides. Also verify that the configured Drive root folder is accessible for future uploads; its ID is deliberately not changed by account repair.

If a file opens in the correct account's browser but still returns 404 through the app, investigate its per-file OAuth access and the original OAuth client before changing scopes or re-uploading. Preserve the local original. Only resume checksum-verified local cleanup after access has been established.

## Verification status

Six synthetic helper tests pass: credential replacement preserves unrelated settings, invalid bundles are rejected, wrong accounts do not export credentials, failed private-directory protection does not export credentials, successful exports keep configs unchanged and omit tokens from output, and library exception details cannot expose secrets. Actual Windows directory protection was verified twice. Browser authorization, Contabo apply and document access recovery have not been performed yet.
