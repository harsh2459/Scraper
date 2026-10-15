"""
Background auto-fetch.

Calls the scraper's /api/scrape-appno-batch — one worker claims a BATCH of
pending trademarks and hands the whole list to the scraper in a single HTTP
call. The scraper then holds ONE warm/cold session for the entire batch and
loops through every app_no in-process (see app.py's _run_batch_job), exactly
like the scraper's own "Auto Discover" feature does. This replaces the old
one-HTTP-job-per-mark + 5s-status-poll pattern, which paid a claim/release
cycle and repeated Flask round-trips for every single trademark — that
per-mark overhead was the actual reason this queue felt much slower than
Auto Discover even with the same warm-pool accounts underneath both.

_WORKERS controls how many *batches* can be in flight at once, not how many
individual marks — keep it <= (warm accounts + usable cold accounts) so
concurrent batches don't just queue on "no free email pool account".
"""

import datetime
import logging
import threading
import time

import requests
import sentry_sdk

import config
import db
import digest_email

import os as _os


def _envbool(key: str, default: bool) -> bool:
    return _os.environ.get(key, "1" if default else "0") == "1"


def _envint(key: str, default: int) -> int:
    try:
        return int(_os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


# ON by default now. The scraper's warm session pool + the >2-account OTP pool
# remove the contention that made this a problem before. Override in the .env
# with AUTO_FETCH_ENABLED=0 to pause.
_AUTO_FETCH_ENABLED = _envbool("AUTO_FETCH_ENABLED", True)

# How many BATCHES can be in flight concurrently — each one holds one
# scraper session (warm or cold) for its whole batch, so this is effectively
# "how many accounts get used at once". In fleet mode this is now just the
# fallback (see _resolve_worker_count): the real count is however many
# accounts are assigned to this machine, so it scales automatically as
# accounts are added/removed on the dashboard — no per-machine .env tuning.
_WORKERS = _envint("AUTO_FETCH_WORKERS", 2)

# How many trademarks one batch call covers. The scraper caps this too
# (_BATCH_MAX_APP_NOS in app.py) — keep in sync if you change one.
_BATCH_SIZE = _envint("AUTO_FETCH_BATCH_SIZE", 15)

# Gap a worker waits after finishing one batch before claiming the next.
_LAUNCH_GAP_SECONDS = _envint("AUTO_FETCH_GAP_SECONDS", 3)
_IDLE_POLL_SECONDS = 10                  # how often a worker checks for new pending work
_RETRY_BACKOFF_SECONDS = 60              # backoff after an unexpected worker-loop failure
_BATCH_POLL_INTERVAL_SECONDS = 3         # how often to check on an in-flight batch
# Give a batch of _BATCH_SIZE marks generous room: ~30s/mark worst case
# (cold-ish) plus login overhead, so a full batch never times out early.
_BATCH_POLL_TIMEOUT_SECONDS = 60 + _BATCH_SIZE * 30

# Failure messages that reflect shared-resource contention or transient
# infra hiccups — not something wrong with the specific application number —
# so they shouldn't burn into the per-row auto-retry budget
# (db.mark_trademark_error's retryable=True rolls attempt_count back).
# The 15-account OTP pool is shared with the main scraper app's own
# concurrent series-scanner, so "no free pool account" / "already logged in
# on another tab" happen routinely under load and clear up on their own.
_RETRYABLE_ERROR_MARKERS = (
    "no free email pool account",
    "could not reach scraper app",
    "already logged in on another tab",
    "previous logout was not clean",
    "scrape job was lost",
    "timed out waiting for the scrape",
    "otp not received",
    "otp already sent",
    "portal is currently blocking this machine",
)


def _is_retryable(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _RETRYABLE_ERROR_MARKERS)


def _mark_trademark_error(conn, id_: int, app_no: str, message: str, retryable: bool) -> None:
    """Thin wrapper around db.mark_trademark_error that also tags the
    Sentry scope with app_no/machine_id, so a scraper error shows up in
    Sentry as structured, searchable data ("these 3 error types happen at
    this rate, mostly on these 2 fleet machines") instead of only ever
    being visible by grepping the log file."""
    with sentry_sdk.push_scope() as scope:
        scope.set_tag("app_no", app_no)
        scope.set_tag("retryable", retryable)
        if config.FLEET_MODE_ENABLED and config.FLEET_MACHINE_KEY:
            scope.set_tag("machine_key", config.FLEET_MACHINE_KEY)
        sentry_sdk.capture_message(message, level="warning" if retryable else "error")
    db.mark_trademark_error(conn, id_, message, retryable=retryable)


# ── Fleet worker mode (Contabo-coordinator + local-worker-fleet) ────────────
# Resolved once per process — this instance's own fleet_machines.id, looked
# up by config.FLEET_MACHINE_KEY directly against the shared Postgres this
# tm_manager already connects to (fleet_machines is tm_manager's own table,
# no cross-schema concern here unlike the scraper's copy of this pattern in
# H:\Datascraper\app.py). None whenever config.FLEET_MODE_ENABLED is off —
# every existing single-machine install, and Contabo itself.
_fleet_machine_id: int | None = None


def _resolve_fleet_machine_id() -> int | None:
    global _fleet_machine_id
    if not config.FLEET_MODE_ENABLED:
        return None
    if _fleet_machine_id is not None:
        return _fleet_machine_id
    if not config.FLEET_MACHINE_KEY:
        logging.warning("Fleet: FLEET_MODE_ENABLED is on but FLEET_MACHINE_KEY is not set.")
        return None
    conn = db.get_connection()
    try:
        machine = db.upsert_fleet_machine(conn, config.FLEET_MACHINE_KEY)
    finally:
        conn.close()
    _fleet_machine_id = machine["id"]
    logging.info("Fleet: this machine registered/resolved as id=%d (machine_key=%r).", _fleet_machine_id, config.FLEET_MACHINE_KEY)
    return _fleet_machine_id


_FLEET_HEARTBEAT_INTERVAL_SECONDS = 45
_last_fleet_heartbeat_at = 0.0


def _maybe_send_fleet_heartbeat() -> None:
    """Best-effort — a missed heartbeat just makes this machine look
    briefly offline on the dashboard, never affects fetch correctness."""
    global _last_fleet_heartbeat_at
    now = time.time()
    if now - _last_fleet_heartbeat_at < _FLEET_HEARTBEAT_INTERVAL_SECONDS:
        return
    _last_fleet_heartbeat_at = now
    try:
        conn = db.get_connection()
        try:
            db.record_fleet_heartbeat(conn, config.FLEET_MACHINE_KEY, {})
        finally:
            conn.close()
    except Exception:
        logging.exception("Fleet: heartbeat failed — will retry next tick")


def _fleet_heartbeat_loop() -> None:
    """Own dedicated thread for the heartbeat, independent of _worker_loop.
    A single busy worker (AUTO_FETCH_WORKERS=1) can sit inside one slow
    _run_batch call — CAPTCHA + OTP wait + poll loop — for minutes at a
    time without ever returning to the top of _worker_loop's while-loop,
    which is where the heartbeat used to be sent from. That starved the
    heartbeat exactly when a low worker count made it matter most (2026-09-
    23: throttling AUTO_FETCH_WORKERS down to fix an IP-India rate-limit
    issue made the fleet dashboard show this machine as 'Offline' even
    though it was actively, correctly scraping). No-ops entirely when
    FLEET_MODE_ENABLED is off, so this adds nothing for a non-fleet
    install — same guarantee _maybe_send_fleet_heartbeat's call sites
    always had, just centralized here instead of depending on the worker
    loop's own pacing."""
    while True:
        if config.FLEET_MODE_ENABLED:
            _maybe_send_fleet_heartbeat()
        time.sleep(_FLEET_HEARTBEAT_INTERVAL_SECONDS)


# The scraper app requires HTTP Basic auth on every request (see its
# _require_basic_auth before_request hook) once BASIC_AUTH_USER/PASS are
# set — which they are in production. Without this, every single batch
# call here comes back HTTP 401 and auto-fetch silently does nothing
# forever (each 401 is logged as "scraper declined batch" and backed off
# 60s, indistinguishable from a real decline unless you read the message).
# Both services share the same .env credentials, so reuse them here.
_SCRAPER_AUTH = (
    (config.BASIC_AUTH_USER, config.BASIC_AUTH_PASS)
    if config.BASIC_AUTH_USER and config.BASIC_AUTH_PASS
    else None
)


def _safe_json(resp) -> dict:
    """resp.json() but never raises. A non-2xx response can come back with a
    body that isn't JSON at all (nginx's own error page, waitress choking
    mid-restart, a bare 500 with an HTML traceback) — .json() throws
    JSONDecodeError on that, which previously crashed the whole worker loop
    (seen live: 2026-09-11, scraper mid-restart when a batch POST landed).
    Returns {} on any parse failure so callers can .get(...) safely."""
    try:
        return resp.json()
    except ValueError:
        return {}


def _report_fleet_result(conn, my_trademark_id: int, status: str) -> None:
    """Fleet-mode only, best-effort — mirrors this row's real outcome
    (already durably written to my_trademarks by the caller) onto today's
    fleet_assignments row, purely for the dashboard's pending/done/failed
    counts. Direct DB write (this worker's tm_manager already has one),
    not an HTTP call to fleet_api.py — see the plan doc's Phase 2
    implementation note for why. A failure here never affects fetch
    correctness, so it's swallowed rather than raised."""
    if not config.FLEET_MODE_ENABLED:
        return
    try:
        conn.execute(
            "UPDATE fleet_assignments SET status = %(status)s, completed_at = now() "
            "WHERE my_trademark_id = %(tm_id)s AND work_date = current_date AND status IN ('assigned', 'claimed')",
            {"status": status, "tm_id": my_trademark_id},
        )
        conn.commit()
    except Exception:
        logging.exception("Fleet: could not report result for my_trademark_id=%s", my_trademark_id)


def _apply_batch_result(conn, rows_by_app_no: dict[str, dict], results: dict) -> None:
    """Write each batch item's outcome back onto its my_trademarks row. Marks
    with no entry in `results` (batch died mid-way — see app.py's
    SessionExpiredError break) are left as 'fetching' so the stale-fetching
    reaper / next claim cycle picks them back up rather than silently
    stranding them."""
    for app_no, item in results.items():
        row = rows_by_app_no.get(app_no)
        if row is None:
            continue
        outcome = item.get("outcome")
        if outcome == "hit":
            search_id = item["search_id"]
            if db.has_required_trademark_fields(conn, app_no, search_id):
                db.mark_trademark_fetched(conn, row["id"], search_id)
                _report_fleet_result(conn, row["id"], "done")
                logging.info("Auto-fetch: %s fetched (search_id=%s)", app_no, search_id)
            else:
                # Portal matched this app_no but key fields (Application
                # Date, Class) are still blank — a partial parse, not a
                # real fetch (see has_required_trademark_fields). Treat
                # like a retryable miss so the next claim cycle tries
                # again instead of permanently showing a half-empty record.
                _mark_trademark_error(
                    conn, row["id"], app_no,
                    "Portal returned a partial record (missing key fields) — will retry.",
                    retryable=True,
                )
                _report_fleet_result(conn, row["id"], "failed")
                logging.warning(
                    "Auto-fetch: %s hit but missing required fields (search_id=%s) — will retry",
                    app_no, search_id,
                )
        elif outcome == "miss":
            message = item.get("message") or "No results found."
            _mark_trademark_error(conn, row["id"], app_no, message, retryable=False)
            _report_fleet_result(conn, row["id"], "failed")
        else:  # "error"
            message = item.get("message") or "Batch scrape failed."
            _mark_trademark_error(conn, row["id"], app_no, message, retryable=_is_retryable(message))
            _report_fleet_result(conn, row["id"], "failed")


def _run_batch(conn, rows: list[dict]) -> bool:
    """Hand a batch of claimed trademarks to the scraper's
    /api/scrape-appno-batch — one held session covers the whole list (see
    this module's docstring for why that matters for speed).

    Returns True if the batch actually ran (even if individual marks inside
    it failed), False if it couldn't even start (scraper unreachable, or
    declined outright — e.g. accounts still cooling down). The caller uses
    this to back off before claiming another batch, instead of firing a new
    one every _LAUNCH_GAP_SECONDS regardless — that fixed-gap retry is what
    caused a real OTP-collision storm on 2026-09-11: a batch that fails
    instantly (no free pool account) returned almost immediately, so the
    next batch started ~3s later and raced fresh login attempts against
    accounts that were still mid-cooldown from the first attempt.
    """
    app_nos = [r["app_no"] for r in rows]
    rows_by_app_no = {r["app_no"]: r for r in rows}
    applied: set[str] = set()   # app_nos already written back, so a re-poll doesn't double-apply

    try:
        resp = requests.post(
            f"{config.SCRAPER_API_BASE_URL}/api/scrape-appno-batch",
            json={"app_nos": app_nos, "requested_by": "tm_manager"},
            timeout=30,
            auth=_SCRAPER_AUTH,
        )
    except requests.RequestException as exc:
        message = f"Could not reach scraper app: {exc}"
        logging.warning("Auto-fetch: batch of %d could not reach scraper: %s", len(app_nos), exc)
        for row in rows:
            _mark_trademark_error(conn, row["id"], row["app_no"], message, retryable=_is_retryable(message))
        return False

    if resp.status_code != 202:
        message = _safe_json(resp).get("error") or f"HTTP {resp.status_code}"
        logging.info("Auto-fetch: scraper declined batch of %d — %s", len(app_nos), message)
        for row in rows:
            _mark_trademark_error(conn, row["id"], row["app_no"], message, retryable=_is_retryable(message))
        return False

    job_id = _safe_json(resp).get("job_id")
    if not job_id:
        message = f"Scraper accepted the batch but returned no job_id (HTTP {resp.status_code})."
        logging.warning("Auto-fetch: %s", message)
        for row in rows:
            _mark_trademark_error(conn, row["id"], row["app_no"], message, retryable=True)
        return False
    for row in rows:
        db.mark_trademark_job_started(conn, row["id"], job_id)
    logging.info("Auto-fetch: started batch job %s for %d mark(s)", job_id, len(app_nos))

    elapsed = 0
    while elapsed < _BATCH_POLL_TIMEOUT_SECONDS:
        time.sleep(_BATCH_POLL_INTERVAL_SECONDS)
        elapsed += _BATCH_POLL_INTERVAL_SECONDS
        try:
            status_resp = requests.get(
                f"{config.SCRAPER_API_BASE_URL}/api/scrape-appno-batch/{job_id}", timeout=15,
                auth=_SCRAPER_AUTH,
            )
        except requests.RequestException as exc:
            logging.warning("Auto-fetch: batch status check failed (job %s): %s", job_id, exc)
            continue
        status = _safe_json(status_resp)
        if not status:
            # scraper returned something we couldn't parse (mid-restart,
            # nginx error page, etc.) — treat as "still running", try again
            # next poll instead of crashing the loop.
            logging.warning("Auto-fetch: batch status for job %s returned unparseable body (HTTP %s) — will re-check", job_id, status_resp.status_code)
            continue

        if status.get("status") == "unknown":
            message = "Batch job was lost (scraper app may have restarted) — will retry."
            for row in rows:
                _mark_trademark_error(conn, row["id"], row["app_no"], message, retryable=True)
            return True

        results = status.get("results") or {}
        # Apply whatever's landed so far — a batch reports results
        # incrementally as each app_no finishes, not just at the end, so
        # earlier marks in the batch update immediately rather than waiting
        # for the slowest one. `applied` prevents double-writing on re-polls.
        new_results = {a: r for a, r in results.items() if a in rows_by_app_no and a not in applied}
        if new_results:
            _apply_batch_result(conn, rows_by_app_no, new_results)
            applied.update(new_results)

        if status.get("status") == "done":
            return True

        # The scraper's own job may keep running well past this — it
        # refills itself with MORE of this machine's assigned work on the
        # same login session once its handed-over list runs out (see
        # app.py's _run_batch_job / _claim_more_from_tm_manager), so
        # status["status"] can legitimately stay "running" long after
        # every mark THIS poll call cares about has already resolved.
        # Returning here as soon as our own slice is fully applied avoids
        # sitting idle for the rest of _BATCH_POLL_TIMEOUT_SECONDS doing
        # nothing useful — the next worker tick just claims a fresh slice.
        if len(applied) >= len(rows_by_app_no):
            return True

    logging.warning("Auto-fetch: timed out waiting for batch job %s", job_id)
    message = "Timed out waiting for the batch scrape to finish — will retry."
    for app_no, row in rows_by_app_no.items():
        if app_no not in applied:
            _mark_trademark_error(conn, row["id"], app_no, message, retryable=True)
    return True


_FAST_ADD_POLL_INTERVAL_SECONDS = 2
# Was 180s — far too tight in practice. A single cold login already takes
# 60-90s (CAPTCHA solve + OTP send/receive/verify, see
# ipindia_scraper.authenticate_pool_account), and the scraper's own
# /api/scrape-appno retry loop (_claim_and_launch_with_retry) can burn
# through more than one such attempt if the first pool account it claims
# hits a cooldown/contention error — each retry adds another 60-100s
# backoff on top. 180s routinely expired WHILE the scraper was still
# genuinely working, marking the mark 'error' here even though the fetch
# went on to succeed moments later (confirmed live 2026-09-21: app_no
# 7870796 showed status='error' with "Timed out waiting for the fetch to
# finish" yet already had a real tm-a-trade-marks-application.pdf on
# disk under Documents — proof the scraper's job actually completed after
# this poll loop had already given up). This is very likely the main
# cause behind "5 attempts, only 1-2 succeed" — most of those "failures"
# were premature timeouts here, not real scrape failures. Sized to match
# the batch path's own budget (_BATCH_POLL_TIMEOUT_SECONDS, ~510s for a
# 15-mark batch) rather than guessing a smaller number for the single-item
# case, since the same login/retry costs apply regardless of batch size.
_FAST_ADD_POLL_TIMEOUT_SECONDS = 480


def _run_fast_fetch_one(app_no: str, id_: int) -> None:
    """Fetch one just-added trademark right away, at the same priority as a
    live TM Search — calls the scraper's single-item /api/scrape-appno (NOT
    the batch endpoint), so it never waits behind the batch worker's queue
    and is allowed to take the last free cold account if that's all that's
    left (see db.claim_free_pool_account's for_batch_worker flag). This is
    what makes "Add to portfolio" feel fast instead of leaving the new mark
    to be picked up whenever the slow batch worker gets to it."""
    conn = db.get_connection()
    try:
        try:
            resp = requests.post(
                f"{config.SCRAPER_API_BASE_URL}/api/scrape-appno",
                json={"app_no": app_no, "search_type": "appno", "requested_by": "tm_manager_fast_add"},
                timeout=15,
                auth=_SCRAPER_AUTH,
            )
        except requests.RequestException as exc:
            message = f"Could not reach scraper app: {exc}"
            logging.warning("Fast-add fetch: %s could not reach scraper: %s", app_no, exc)
            _mark_trademark_error(conn, id_, app_no, message, retryable=_is_retryable(message))
            return

        if resp.status_code != 202:
            message = _safe_json(resp).get("error") or f"HTTP {resp.status_code}"
            logging.info("Fast-add fetch: scraper declined %s — %s", app_no, message)
            _mark_trademark_error(conn, id_, app_no, message, retryable=_is_retryable(message))
            return

        job_id = _safe_json(resp).get("job_id")
        if not job_id:
            message = f"Scraper accepted the request but returned no job_id (HTTP {resp.status_code})."
            logging.warning("Fast-add fetch: %s", message)
            _mark_trademark_error(conn, id_, app_no, message, retryable=True)
            return
        db.mark_trademark_job_started(conn, id_, job_id)

        elapsed = 0
        while elapsed < _FAST_ADD_POLL_TIMEOUT_SECONDS:
            time.sleep(_FAST_ADD_POLL_INTERVAL_SECONDS)
            elapsed += _FAST_ADD_POLL_INTERVAL_SECONDS
            try:
                status_resp = requests.get(
                    f"{config.SCRAPER_API_BASE_URL}/api/scrape-status/{job_id}", timeout=15,
                    auth=_SCRAPER_AUTH,
                )
            except requests.RequestException as exc:
                logging.warning("Fast-add fetch: status check failed (job %s): %s", job_id, exc)
                continue
            status = _safe_json(status_resp)
            if not status:
                continue

            job_status = status.get("status")
            if job_status == "unknown":
                message = "Fetch job was lost (scraper app may have restarted) — will retry."
                _mark_trademark_error(conn, id_, app_no, message, retryable=True)
                return
            if job_status == "done":
                search_id = status.get("search_id")
                if search_id and db.has_required_trademark_fields(conn, app_no, search_id):
                    db.mark_trademark_fetched(conn, id_, search_id)
                    logging.info("Fast-add fetch: %s fetched (search_id=%s)", app_no, search_id)
                else:
                    _mark_trademark_error(
                        conn, id_, app_no,
                        "Portal returned a partial record (missing key fields) — will retry.",
                        retryable=True,
                    )
                return
            if job_status == "error":
                message = status.get("message") or "Fetch failed."
                _mark_trademark_error(conn, id_, app_no, message, retryable=_is_retryable(message))
                return
            # still running — keep polling

        logging.warning("Fast-add fetch: timed out waiting for job %s (%s)", job_id, app_no)
        _mark_trademark_error(
            conn, id_, app_no, "Timed out waiting for the fetch to finish — will retry.", retryable=True,
        )
    except Exception:
        logging.exception("Fast-add fetch crashed for %s", app_no)
        try:
            _mark_trademark_error(conn, id_, app_no, "Fast fetch crashed unexpectedly — will retry.", retryable=True)
        except Exception:
            pass
    finally:
        conn.close()


def fast_fetch_new_trademarks(added_ids: dict[str, int]) -> None:
    """Kick off an immediate, live-priority fetch for each newly-added
    trademark, one background thread per app_no, so /api/add can return to
    the UI right away while these run. Called from /api/add right after the
    rows are written as 'pending' — this is what makes a freshly-added mark
    show real data in seconds instead of waiting for the batch worker's
    slow, patient queue to eventually reach it."""
    for app_no, id_ in added_ids.items():
        threading.Thread(
            target=_run_fast_fetch_one, args=(app_no, id_),
            name=f"fast-add-{app_no}", daemon=True,
        ).start()


def _worker_loop() -> None:
    while True:
        try:
            if not _AUTO_FETCH_ENABLED:
                time.sleep(_IDLE_POLL_SECONDS)
                continue

            # Heartbeat is sent from its own dedicated thread now (see
            # _fleet_heartbeat_loop) — no longer sent from here, since this
            # loop can sit blocked inside one slow _run_batch call for
            # minutes, which used to starve the heartbeat right when a low
            # worker count made it matter most.

            conn = db.get_connection()
            try:
                if config.FLEET_MODE_ENABLED:
                    machine_id = _resolve_fleet_machine_id()
                    # Not registered yet (transient — see
                    # _resolve_fleet_machine_id) — nothing to claim this
                    # tick, NOT a fallback to the global queue, since that
                    # would defeat the whole point of fleet scoping the
                    # moment registration is briefly unavailable.
                    rows = db.claim_fleet_assigned_batch(conn, machine_id, _BATCH_SIZE) if machine_id else []
                else:
                    rows = db.claim_batch_pending_trademarks(conn, _BATCH_SIZE)
                if not rows:
                    conn.close()
                    time.sleep(_IDLE_POLL_SECONDS)
                    continue
                started = _run_batch(conn, rows)
            finally:
                conn.close()

            if started:
                # Steady gap before the next claim — keeps logins staggered
                # instead of immediately racing for the next pool account.
                time.sleep(_LAUNCH_GAP_SECONDS)
            else:
                # The batch never got a session at all (scraper unreachable,
                # or the scraper's own retry/backoff already exhausted every
                # account). Back off hard here too — see _run_batch's
                # docstring for the storm this prevents.
                logging.warning("Auto-fetch: batch couldn't start — backing off %ds before next claim", _RETRY_BACKOFF_SECONDS)
                time.sleep(_RETRY_BACKOFF_SECONDS)
        except Exception:
            logging.exception("Auto-fetch worker tick failed — will retry")
            time.sleep(_RETRY_BACKOFF_SECONDS)


_STALENESS_CHECK_INTERVAL_SECONDS = 900  # re-queue stale marks every 15 min


# How long an 'error' row sits untouched before it gets a fresh retry
# budget. Without this, a mark that burned through _MAX_AUTO_RETRY_ATTEMPTS
# during a rough patch (e.g. a broken warm account) is excluded from
# claim_batch_pending_trademarks forever — nothing but a manual Refetch
# click would ever pick it up again. See db.requeue_old_errors.
_ERROR_REQUEUE_AFTER_HOURS = _envint("AUTO_FETCH_ERROR_REQUEUE_HOURS", 6)
_ERROR_REQUEUE_BATCH = _envint("AUTO_FETCH_ERROR_REQUEUE_BATCH", 100)


def _staleness_loop() -> None:
    """Periodically move marks older than config.REFRESH_INTERVAL_DAYS from
    'fetched' back to 'pending' so the worker refreshes them — and every
    refresh writes a tm_status_history snapshot, so status changes are
    captured automatically without anyone clicking Live refresh. Also
    re-queues 'error' rows that exhausted their auto-retry budget and have
    sat untouched for _ERROR_REQUEUE_AFTER_HOURS, so a rough patch (pool
    outage, broken account) never permanently strands rows in 'error'.

    Gated on _AUTO_FETCH_ENABLED: at cold-login speed this would flood a
    tiny OTP pool, so it only runs when auto-fetch (and ideally the warm
    pool) is on.
    """
    while True:
        try:
            if not _AUTO_FETCH_ENABLED:
                time.sleep(_IDLE_POLL_SECONDS)
                continue
            days = getattr(config, "REFRESH_INTERVAL_DAYS", 7)
            batch = getattr(config, "STALE_REFRESH_BATCH", 20)
            conn = db.get_connection()
            try:
                n = db.requeue_stale_trademarks(conn, days, batch)
                n_err = db.requeue_old_errors(conn, _ERROR_REQUEUE_AFTER_HOURS, _ERROR_REQUEUE_BATCH)
            finally:
                conn.close()
            if n:
                logging.info("Staleness refresh: re-queued %d mark(s) older than %d day(s).", n, days)
            if n_err:
                logging.info(
                    "Error requeue: re-queued %d mark(s) stuck in 'error' for over %dh.",
                    n_err, _ERROR_REQUEUE_AFTER_HOURS,
                )
        except Exception:
            logging.exception("Staleness refresh tick failed")
        time.sleep(_STALENESS_CHECK_INTERVAL_SECONDS)


def _journal_fetch_worker_loop() -> None:
    import subprocess
    import os
    import sys
    _JOURNAL_FETCH_INTERVAL_SECONDS = 86400  # 24 hours
    
    while True:
        try:
            if not _AUTO_FETCH_ENABLED:
                time.sleep(_IDLE_POLL_SECONDS)
                continue
                
            logging.info("Auto-fetch: running scheduled journal check")
            script_path = os.path.join(os.path.dirname(__file__), 'scripts', 'auto_fetch_journal.py')
            subprocess.run([sys.executable, script_path], check=False)
            
        except Exception:
            logging.exception("Auto-fetch journal check failed")
            
        # Sleep for 24 hours before checking again
        time.sleep(_JOURNAL_FETCH_INTERVAL_SECONDS)


_DIGEST_CHECK_INTERVAL_SECONDS = 600  # check every 10 min whether it's time to send


def _digest_loop() -> None:
    """Sends each user's daily digest (status changes, compliance notices,
    renewals due soon) once per calendar day,
    right after the clock crosses config.DIGEST_SEND_HOUR (default 7am
    server time) — checked every 10 minutes rather than sleeping a flat 24h,
    so the send time doesn't drift with process restarts. Per-user
    idempotency comes from users.last_digest_sent_at: a user is only ever
    sent to once their last send was NOT already today, so a restart
    mid-window (or this loop firing more than once past the hour) can never
    double-send — it just finds nothing left to do.
    """
    while True:
        try:
            now = datetime.datetime.now()
            if now.hour >= config.DIGEST_SEND_HOUR and config.is_digest_email_configured():
                conn = db.get_connection()
                try:
                    users = db.list_active_users_for_digest(conn)
                finally:
                    conn.close()

                today = now.date()
                sent = 0
                for u in users:
                    last_sent = u["last_digest_sent_at"]
                    if last_sent is not None and last_sent.date() >= today:
                        continue  # already sent today

                    since = last_sent or (now - datetime.timedelta(days=1))
                    conn = db.get_connection()
                    try:
                        changes = db.list_status_changes_since(conn, u["id"], since)
                        # exclude_done=True: don't keep emailing about a
                        # compliance item the user already marked Completed/
                        # Closed on the trademark's Tasks tab (see
                        # db.set_compliance_status) — the calendar/Tasks tab
                        # still show it (full history there), only the
                        # digest stops nagging once it's handled.
                        compliance = db.list_compliance_notices(conn, u["id"], days_ahead=30, exclude_done=True)
                        renewals = db.upcoming_renewals(conn, u["id"], days_ahead=30)
                        new_apps_row = conn.execute(
                            "SELECT count(*) AS n FROM my_trademarks WHERE user_id = %(uid)s AND created_at >= %(since)s",
                            {"uid": u["id"], "since": now - datetime.timedelta(days=1)},
                        ).fetchone()
                        new_applications_count = new_apps_row["n"] if new_apps_row else 0

                        has_anything = bool(changes) or any(compliance.values()) or bool(renewals)
                        if has_anything:
                            ok = digest_email.send_digest(
                                u["email"], changes, compliance, renewals, new_applications_count
                            )
                            if ok:
                                db.mark_digest_sent(conn, u["id"], now)
                                sent += 1
                        else:
                            # Nothing to report — still mark as sent for today
                            # so this user isn't re-checked every 10 min for
                            # the rest of the day, and tomorrow's `since`
                            # starts from now rather than growing unbounded.
                            db.mark_digest_sent(conn, u["id"], now)
                    finally:
                        conn.close()
                if sent:
                    logging.info("Digest: sent %d email(s).", sent)
        except Exception:
            logging.exception("Digest loop tick failed")
        time.sleep(_DIGEST_CHECK_INTERVAL_SECONDS)


def _resolve_worker_count() -> int:
    """Fleet mode: size AUTO_FETCH_WORKERS to however many accounts are
    actually assigned to THIS machine right now, instead of a fixed .env
    number that has to be hand-tuned per machine every time an account is
    added or removed on the dashboard. One worker per assigned account is
    the natural ceiling anyway — a machine can never usefully run more
    concurrent batches than it has accounts to hold sessions on, since
    claim_free_pool_account/claim_warm_slot only ever hand out accounts
    this machine owns. Falls back to the .env AUTO_FETCH_WORKERS value
    whenever fleet mode is off (non-fleet installs, or a fleet machine not
    registered yet) or the account count can't be read — never silently
    runs with 0 workers."""
    if not config.FLEET_MODE_ENABLED:
        return max(1, _WORKERS)
    machine_id = _resolve_fleet_machine_id()
    if machine_id is None:
        return max(1, _WORKERS)
    try:
        conn = db.get_connection()
        try:
            n = db.count_assigned_accounts(conn, machine_id)
        finally:
            conn.close()
    except Exception:
        logging.exception("Fleet: could not count assigned accounts — falling back to AUTO_FETCH_WORKERS=%d", _WORKERS)
        return max(1, _WORKERS)
    return max(1, n)


def start() -> None:
    if not config.BACKGROUND_TASKS_ENABLED:
        return
    if config.FLEET_CONTROL_ENABLED:
        # User service retains notifications/journal tasks, but never competes
        # with the control plane for scheduling or fleet ownership.
        threading.Thread(target=_journal_fetch_worker_loop, daemon=True).start()
        threading.Thread(target=_digest_loop, daemon=True).start()
        return
    worker_count = _resolve_worker_count()
    for i in range(worker_count):
        threading.Thread(target=_worker_loop, name=f"autofetch-{i}", daemon=True).start()
    threading.Thread(target=_journal_fetch_worker_loop, daemon=True).start()
    threading.Thread(target=_staleness_loop, daemon=True).start()
    threading.Thread(target=_digest_loop, daemon=True).start()
    threading.Thread(target=_fleet_heartbeat_loop, name="fleet-heartbeat", daemon=True).start()
    logging.info(
        "TM Manager auto-fetch started: %d worker(s), %ds gap (enabled=%s). "
        "Journal + staleness workers scheduled.",
        worker_count, _LAUNCH_GAP_SECONDS, _AUTO_FETCH_ENABLED,
    )
