"""
Warm session pool — keeps a set of otp_email_pool accounts
(reserved_for = 'warm_pool', see db.py) permanently logged in to IP India so
an appno search can skip the ~60-90s cold-login cost (CAPTCHA solve + OTP
send/receive/verify) entirely and go straight to the per-search CAPTCHA +
actual search.

Ownership model: unlike the cold pool (claim -> use once -> release back to
the shared pool, see db.claim_free_pool_account), a warm-reserved account
NEVER goes back to the cold pool. It cycles between this module's own
ready/claimed/logging_in states for as long as the process runs. Wiped on
every process restart — start() re-logs in all warm slots from scratch;
searches simply fall back to the cold path until slots come back ready.

Hardening (see the system-hardening plan Phase 1B):
- WARM_POOL_ENABLED gate + WARM_POOL_MAX_CONCURRENT cap
- liveness probe before handing a slot to a search (catches silently-dead
  sessions instead of failing the search with SessionExpiredError)
- proactive background re-login before WARM_POOL_MAX_SESSION_AGE_SECONDS so a
  session never gets old enough to hit the portal's absolute session-age limit
- clean logout (sess.logout()) on death so the account isn't left 'unclean'
  (which blocks its next login for ~90s)
- runtime add_slot()/remove_slot() for the /api/pool/accounts/<email>/role
  endpoint (change the warm/cold split without a restart)
"""

import logging
import re
import threading
import time

import config
import db
from ipindia_scraper import IPIndiaSession, authenticate_pool_account

log = logging.getLogger(__name__)

# Resolved once, mirrors app.py's own _resolve_fleet_machine_id — kept as a
# small local copy (not imported from app.py) to avoid a circular import,
# since app.py is the one that imports warm_pool, never the reverse. None
# whenever config.FLEET_MODE_ENABLED is off, which is every existing
# single-machine install and Contabo itself (never runs in fleet mode) —
# see claim_free_pool_account/list_warm_pool_accounts's own machine_id
# docstrings for what this changes.
_fleet_machine_id: int | None = None


def _resolve_fleet_machine_id() -> int | None:
    global _fleet_machine_id
    if not config.FLEET_MODE_ENABLED:
        return None
    if _fleet_machine_id is not None:
        return _fleet_machine_id
    if not config.FLEET_MACHINE_KEY:
        log.warning("Fleet: FLEET_MODE_ENABLED is on but FLEET_MACHINE_KEY is not set.")
        return None
    conn = db.get_connection()
    try:
        try:
            row = conn.execute(
                "SELECT id FROM fleet_machines WHERE machine_key = %(k)s", {"k": config.FLEET_MACHINE_KEY},
            ).fetchone()
        except Exception:
            # See app.py's _resolve_fleet_machine_id for why this can
            # legitimately not exist yet (fleet_machines is tm_manager's
            # table, not this scraper's own schema).
            log.warning("Fleet: could not query fleet_machines yet — falling back to unscoped.", exc_info=True)
            return None
    finally:
        conn.close()
    if row is None:
        log.warning("Fleet: machine_key %r not found in fleet_machines yet.", config.FLEET_MACHINE_KEY)
        return None
    _fleet_machine_id = row["id"]
    return _fleet_machine_id


_RELOGIN_RETRY_BACKOFF_SECONDS = 20  # gap between failed re-login attempts
_UNCLEAN_LOGOUT_BACKOFF_SECONDS = 95  # portal explicitly asks for ~90s
_REPEATED_FAILURE_THRESHOLD = 3       # consecutive same-slot failures before escalating
_ESCALATED_BACKOFF_SECONDS = 300      # slower retry once a slot looks stuck (e.g. blocked/rate-limited)

_WAIT_SECONDS_RE = re.compile(r"wait\D{0,10}(\d+)\s*sec", re.IGNORECASE)


def _backoff_for_error(err: str, consecutive_failures: int) -> float:
    """Pick how long to wait before the next attempt, based on what the
    portal actually told us instead of always using the flat default —
    respect an explicit cooldown, and slow down if the same slot keeps
    failing the same way (likely a soft block, not a transient blip)."""
    if err:
        m = _WAIT_SECONDS_RE.search(err)
        if m:
            return max(float(m.group(1)) + 5, _RELOGIN_RETRY_BACKOFF_SECONDS)
        if "not clean" in err.lower():
            return _UNCLEAN_LOGOUT_BACKOFF_SECONDS
    if consecutive_failures >= _REPEATED_FAILURE_THRESHOLD:
        return _ESCALATED_BACKOFF_SECONDS
    return _RELOGIN_RETRY_BACKOFF_SECONDS


class WarmSlot:
    """One reserved pool account's current login state. status is one of:
    'logging_in' (background login in progress, not usable yet),
    'ready' (idle, authenticated, available to claim),
    'claimed' (a search is currently using it),
    'dead' (removed at runtime; will not be re-logged)."""

    def __init__(self, pool_account: dict, is_priority: bool = False):
        self.pool_account = pool_account
        self.status = "logging_in"
        self.sess: IPIndiaSession | None = None
        self.last_verified_at: float = 0.0   # monotonic; set on login + on a good liveness probe
        self.logged_in_at: float = 0.0       # monotonic; for proactive age-based re-login
        # A priority slot is off-limits to the batch auto-fetch queue (see
        # claim_warm_slot's exclude_priority param) — it exists so a manual
        # TM Search never has to wait behind a big batch job tying up every
        # warm account. A batch of 20 marks can easily hold all 8 warm
        # lanes for 10+ minutes; without a reserved lane, a single search
        # started during that window falls back to a slow cold login even
        # though it only needs one account for a few seconds.
        self.is_priority = is_priority


_lock = threading.Lock()
_slots: list[WarmSlot] = []
_started = False
_reaper_started = False


def _login_loop(slot: WarmSlot) -> None:
    """Keep retrying login for this slot until it succeeds. Runs in its own
    daemon thread: once at startup, and again whenever the slot's session is
    retired (release_and_refill on death, or the proactive age reaper)."""
    with _lock:
        if slot.status == "dead":
            return
        slot.status = "logging_in"
        slot.sess = None

    attempt = 0
    last_err = ""
    consecutive_same_error = 0
    while True:
        with _lock:
            if slot.status == "dead":
                return
        attempt += 1
        sess, err = authenticate_pool_account(slot.pool_account)
        if sess is not None:
            now = time.monotonic()
            with _lock:
                if slot.status == "dead":
                    # removed while we were logging in — clean up
                    try:
                        sess.logout()
                    except Exception:
                        pass
                    return
                slot.sess = sess
                slot.status = "ready"
                slot.last_verified_at = now
                slot.logged_in_at = now
            log.info(
                "warm_pool: slot %s (%s) is ready after %d attempt(s)",
                slot.pool_account["id"], slot.pool_account["email_address"], attempt,
            )
            return
        consecutive_same_error = consecutive_same_error + 1 if err == last_err else 1
        last_err = err
        backoff = _backoff_for_error(err, consecutive_same_error)
        log.warning(
            "warm_pool: slot %s (%s) login attempt %d failed: %s (retrying in %ds)",
            slot.pool_account["id"], slot.pool_account["email_address"], attempt, err, backoff,
        )
        time.sleep(backoff)


def _reaper_loop() -> None:
    """Proactively retire + re-login any ready slot older than
    WARM_POOL_MAX_SESSION_AGE_SECONDS, so sessions never age into the
    portal's absolute expiry. Staggered: one slot per pass."""
    while True:
        time.sleep(60)
        max_age = config.WARM_POOL_MAX_SESSION_AGE_SECONDS
        if max_age <= 0:
            continue
        now = time.monotonic()
        target: WarmSlot | None = None
        with _lock:
            for s in _slots:
                if s.status == "ready" and s.logged_in_at and (now - s.logged_in_at) > max_age:
                    target = s
                    s.status = "logging_in"   # claim it out of rotation now
                    break
        if target is None:
            continue
        log.info(
            "warm_pool: slot %s (%s) session aged out (%ds) — proactive re-login",
            target.pool_account["id"], target.pool_account["email_address"],
            int(now - target.logged_in_at),
        )
        old = target.sess
        target.sess = None
        if old is not None:
            try:
                old.logout()
            except Exception:
                pass
        threading.Thread(target=_login_loop, args=(target,), daemon=True).start()


def start() -> None:
    """Load the reserved warm-pool accounts and start logging each one in, in
    the background — call once at process startup. Safe to call twice."""
    global _started, _reaper_started
    with _lock:
        if _started:
            return
        _started = True

    if not config.WARM_POOL_ENABLED:
        log.info("warm_pool: WARM_POOL_ENABLED=0 — warm pool disabled, all fetches use the cold path.")
        return

    conn = db.get_connection()
    try:
        accounts = db.list_warm_pool_accounts(conn, machine_id=_resolve_fleet_machine_id())
    finally:
        conn.close()

    if not accounts:
        log.info("warm_pool: no accounts reserved (reserved_for='warm_pool') — warm pool inert until one is added.")
        return

    priority_count = max(0, config.WARM_POOL_PRIORITY_COUNT)
    with _lock:
        for i, a in enumerate(accounts):
            _slots.append(WarmSlot(a, is_priority=(i < priority_count)))
        slots_snapshot = list(_slots)
    if priority_count:
        log.info(
            "warm_pool: reserving %d of %d warm account(s) for priority (non-batch) use only.",
            min(priority_count, len(accounts)), len(accounts),
        )

    log.info("warm_pool: starting login for %d reserved account(s)…", len(slots_snapshot))
    for slot in slots_snapshot:
        threading.Thread(target=_login_loop, args=(slot,), daemon=True).start()

    with _lock:
        if not _reaper_started:
            _reaper_started = True
            threading.Thread(target=_reaper_loop, daemon=True).start()


def _ready_count_locked() -> int:
    return sum(1 for s in _slots if s.status == "ready")


def _claimed_count_locked() -> int:
    return sum(1 for s in _slots if s.status == "claimed")


def claim_warm_slot(exclude_priority: bool = False) -> WarmSlot | None:
    """Claim one live, ready warm slot, or None if all lanes are busy /
    logging in / the concurrency cap is hit. Before returning a slot whose
    liveness hasn't been confirmed recently, probe it; a dead session is
    marked for re-login and skipped. Callers MUST call
    release_and_refill(slot, died=...) exactly once when done.

    exclude_priority=True skips slots reserved for priority use (see
    WarmSlot.is_priority) — pass this from the batch auto-fetch path so a
    big batch can never exhaust every warm account and starve a manual TM
    Search that comes in mid-batch. Single-search callers should leave this
    False: they're allowed to use ANY ready slot, priority-reserved or not,
    since a priority slot sitting idle while a search waits would defeat
    the point of reserving it.
    """
    if not config.WARM_POOL_ENABLED:
        return None

    cap = config.WARM_POOL_MAX_CONCURRENT
    max_age = config.WARM_POOL_LIVENESS_MAX_AGE_SECONDS

    while True:
        with _lock:
            if cap and _claimed_count_locked() >= cap:
                return None
            candidate = next(
                (s for s in _slots if s.status == "ready" and not (exclude_priority and s.is_priority)),
                None,
            )
            if candidate is None:
                return None
            candidate.status = "claimed"  # take it out of rotation while we probe
            needs_probe = (time.monotonic() - candidate.last_verified_at) > max_age
            sess = candidate.sess

        if not needs_probe:
            return candidate

        alive = False
        try:
            alive = bool(sess and sess.ping_alive())
        except Exception:
            alive = False

        if alive:
            with _lock:
                candidate.last_verified_at = time.monotonic()
            return candidate

        # dead session — retire this slot, spin its re-login, try the next
        log.warning(
            "warm_pool: slot %s (%s) failed liveness probe — re-authenticating, trying next lane",
            candidate.pool_account["id"], candidate.pool_account["email_address"],
        )
        old = candidate.sess
        candidate.sess = None
        if old is not None:
            try:
                old.logout()
            except Exception:
                pass
        threading.Thread(target=_login_loop, args=(candidate,), daemon=True).start()
        # loop again to try another ready slot


def release_and_refill(slot: WarmSlot, died: bool) -> None:
    """Called when a search using a warm slot finishes. died=True (search
    raised SessionExpiredError) -> clean logout + fresh background login,
    slot unusable until that completes. Otherwise straight back to 'ready'.
    """
    if not died:
        with _lock:
            if slot.status != "dead":
                slot.status = "ready"
        return

    log.warning(
        "warm_pool: slot %s (%s) session expired — clean logout + re-authenticating…",
        slot.pool_account["id"], slot.pool_account["email_address"],
    )
    old = slot.sess
    slot.sess = None
    if old is not None:
        try:
            old.logout()
        except Exception:
            try:
                old.stop_keepalive()
            except Exception:
                pass
    threading.Thread(target=_login_loop, args=(slot,), daemon=True).start()


def add_slot(pool_account: dict) -> None:
    """Runtime add of a warm slot (role changed to 'warm' via the API,
    without a restart). Starts its login immediately."""
    with _lock:
        if any(s.pool_account["id"] == pool_account["id"] and s.status != "dead" for s in _slots):
            return
        slot = WarmSlot(pool_account)
        _slots.append(slot)
        # make sure the age reaper is running now that we have a warm slot
        global _reaper_started
        start_reaper = not _reaper_started
        if start_reaper:
            _reaper_started = True
    threading.Thread(target=_login_loop, args=(slot,), daemon=True).start()
    if start_reaper:
        threading.Thread(target=_reaper_loop, daemon=True).start()


def remove_slot(email_address: str) -> None:
    """Runtime removal (role changed to 'cold'). Marks the slot dead so
    _login_loop / reaper stop touching it, logs out its session if any."""
    with _lock:
        victims = [s for s in _slots if s.pool_account["email_address"] == email_address]
        for s in victims:
            s.status = "dead"
        sessions = [s.sess for s in victims if s.sess is not None]
        _slots[:] = [s for s in _slots if s.pool_account["email_address"] != email_address]
    for sess in sessions:
        try:
            sess.logout()
        except Exception:
            pass


def status_snapshot() -> list[dict]:
    """Read-only summary of every warm slot — for the health endpoint."""
    now = time.monotonic()
    with _lock:
        return [
            {
                "account_id": s.pool_account["id"],
                "email": s.pool_account["email_address"],
                "status": s.status,
                "is_priority": s.is_priority,
                "verified_age_secs": round(now - s.last_verified_at, 1) if s.last_verified_at else None,
                "session_age_secs": round(now - s.logged_in_at, 1) if s.logged_in_at else None,
            }
            for s in _slots
        ]


def health() -> dict:
    """Counts + whether we're below the configured minimum ready lanes."""
    with _lock:
        ready = _ready_count_locked()
        total = len(_slots)
        claimed = _claimed_count_locked()
        logging_in = sum(1 for s in _slots if s.status == "logging_in")
    return {
        "enabled": config.WARM_POOL_ENABLED,
        "total": total,
        "ready": ready,
        "claimed": claimed,
        "logging_in": logging_in,
        "min_ready": config.WARM_POOL_MIN_READY,
        "below_min": config.WARM_POOL_ENABLED and total > 0 and ready < config.WARM_POOL_MIN_READY,
        "max_concurrent": config.WARM_POOL_MAX_CONCURRENT or total,
    }
