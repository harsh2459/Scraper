# Fleet Worker Setup

This folder turns a Windows PC into a scraper worker for the trademark
tracking system at `mytmtracker.com`. Contabo (the main server) got its own
IP blocked by IP India, so scraping now happens from worker PCs like this
one instead.

## What this does

- Contabo stays the coordinator: the database, the website, and the "what
  needs fetching" decisions all live there, unchanged.
- This PC does the actual work: logging into IP India, solving CAPTCHAs,
  checking application statuses, using accounts assigned to it via the
  Fleet dashboard.
- Results get written straight back to Contabo's database, so the website
  shows them immediately, no matter which worker PC did the fetching.

## First-time setup on a new PC

1. Copy this whole `shared-scraper` folder onto the new PC (USB, network
   share, whatever's easiest).
2. Open PowerShell in that folder and run:
   ```
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```
3. It will install Python if needed, set up everything, and ask you to
   **name this machine** (e.g. `office-pc-2`, `nirav-laptop`). Pick
   something short and unique - every worker PC needs its own name.
4. When it finishes, go to `https://mytmtracker.com/admin/fleet` and assign
   a few email accounts to this machine's name. Without assigned accounts,
   the worker has nothing to log in with and won't do anything.

That's it - it starts working automatically from there.

## Checking on it

- **This machine's own live status**: open
  `http://localhost:5001/status` in a browser on this PC. Shows which
  account is fetching right now, and how many successes/failures each
  account has had since the last restart.
- **All machines at once**: `https://mytmtracker.com/admin/fleet` (needs an
  admin login) shows every worker PC, whether it's online, and how much
  work it's gotten through today.

## Starting and stopping

Setup already starts everything. For later restarts (after a reboot, etc.):

```
powershell -ExecutionPolicy Bypass -File .\run-worker.ps1
```

To stop:

```
powershell -ExecutionPolicy Bypass -File .\run-worker.ps1 -Stop
```

## If something goes wrong

- Check the `.err` log files in this folder and in `tm_manager\` - they're
  plain text, the most recent lines are at the bottom.
- If `setup.ps1` says Python was installed but still can't find it, close
  the PowerShell window, open a fresh one, and run `setup.ps1` again.
- If accounts keep failing to log in with timeouts or "already logged in"
  errors, that's usually normal - IP India's own portal is slow/flaky
  sometimes, and the retry logic handles it. Only worry if NOTHING succeeds
  for a long time (check the status page's success/failure counts).
- If the whole thing seems stuck, `run-worker.ps1 -Stop` then
  `run-worker.ps1` again is safe - nothing is lost, it just picks up where
  it left off from the database.

## What NOT to touch

- Don't edit `.env` or `tm_manager\.env` by hand unless you know what
  you're doing - `setup.ps1` generates these correctly. If you need to
  rename this machine, just re-run `setup.ps1`.
- `AUTO_FETCH_WORKERS` in `tm_manager\.env` controls how many accounts this
  machine uses at once. It starts at 1 (safe, slow) on purpose - IP India
  rate-limits bursts of logins. Only raise it gradually, and watch the
  status page for errors coming back, before pushing it higher.
