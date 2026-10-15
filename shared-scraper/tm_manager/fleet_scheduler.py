"""
Fleet coordinator: daily planning tick (Contabo-coordinator + local-worker-
fleet migration — see the plan doc for the full design).

Runs ONLY on the Contabo process (config.FLEET_COORDINATOR_ENABLED), never
on a worker machine. Once per day, past config.FLEET_PLAN_HOUR, computes
and writes the day's machine<->TM assignment via
db.plan_daily_fleet_assignments — the same function the dashboard's manual
"Replan now" button calls directly, on demand, outside this tick.

This module has nothing to do with fetching — it never talks to the
scraper, never touches my_trademarks.status, and has zero effect on
Contabo's own live-search scraping (Word Mark + Class search, TM Search,
Auto Discover), which keeps running exactly as it always has, untouched by
this migration.
"""

import datetime
import logging
import threading
import time

import config
import db

_CHECK_INTERVAL_SECONDS = 600  # check every 10 min whether it's time to plan today

# In-memory "have we already planned today" guard — acceptable since this
# tick only ever runs on the single Contabo coordinator process (never a
# worker), same tolerance for in-memory scheduling state as this codebase's
# other loops (e.g. scheduler.py's digest loop instead tracks this
# per-user in Postgres because MULTIPLE things read that state; nothing
# else needs to know "did today's plan already run" besides this one loop).
_last_planned_date: datetime.date | None = None


def _plan_tick() -> None:
    global _last_planned_date
    now = datetime.datetime.now()
    today = now.date()
    if _last_planned_date == today:
        return
    if now.hour < config.FLEET_PLAN_HOUR:
        return

    conn = db.get_connection()
    try:
        result = db.plan_daily_fleet_assignments(
            conn, today,
            per_account_daily_cap=config.FLEET_PER_ACCOUNT_DAILY_CAP,
            heartbeat_stale_seconds=config.FLEET_HEARTBEAT_STALE_SECONDS,
        )
    finally:
        conn.close()

    _last_planned_date = today
    logging.info(
        "Fleet: daily plan for %s — %d machine(s), %d due, %d assigned (total weight %d).",
        today, result["machines"], result["due"], result["assigned"], result["total_weight"],
    )


def _plan_loop() -> None:
    while True:
        try:
            _plan_tick()
        except Exception:
            logging.exception("Fleet planning tick failed — will retry")
        time.sleep(_CHECK_INTERVAL_SECONDS)


def start() -> None:
    if not config.FLEET_COORDINATOR_ENABLED:
        return
    threading.Thread(target=_plan_loop, name="fleet-planner", daemon=True).start()
    logging.info(
        "Fleet coordinator enabled: daily planning at hour %d, per-account cap %d/day.",
        config.FLEET_PLAN_HOUR, config.FLEET_PER_ACCOUNT_DAILY_CAP,
    )
