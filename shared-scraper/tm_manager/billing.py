"""
Subscription/billing enforcement — resolves what a user is actually allowed
to do (TM count, seats, search quota, feature flags) from plans +
subscriptions (see db.py's BILLING_SCHEMA), and applies the two behaviors
agreed with the user:

  - No subscription row at all => unlimited. Every pre-existing account
    keeps working exactly as before this system shipped; plans get assigned
    to real accounts afterward, not as a breaking migration.
  - Over the limit (real tm_limit hit, OR a subscription that expired more
    than SUBSCRIPTION_GRACE_DAYS ago) => block new adds only. Existing data
    stays fully visible, editable, and Live-Refreshable — never a full
    read-only lockout.

A seat (users.parent_account_id set) is always billed against its paying
account, never itself — db.resolve_paying_account_id is the one place that
distinction is resolved.

Search metering (check_can_search) only counts manual TM Search lookups
(tm_search_history rows) — NOT Auto Discover, Live Refresh, or Word Mark
search, since those serve a different purpose (portfolio maintenance /
discovery, not "look up one specific application number") and metering
them too would need separate limit fields entirely. The search quota
resets every plan.billing_period_months (admin's choice per plan — 1/3/6/
12, not hardcoded to monthly) — see _current_period_start.
"""

import datetime

import db


def _add_months(dt: datetime.datetime, months: int) -> datetime.datetime:
    """dt shifted forward by *months* calendar months, clamping the day to
    the target month's own last day (e.g. Jan 31 + 1 month -> Feb 28/29,
    never a ValueError from "Feb 31"). Used only to walk a subscription's
    period boundaries forward/back — never needs to be exact to the
    second, just land on the right month."""
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    import calendar
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _current_period_start(anchor: datetime.datetime, period_months: int, now: datetime.datetime) -> datetime.datetime:
    """The most recent billing-period boundary at or before *now*, given a
    subscription's anchor date (started_at, or renews_at if that's more
    reliable for a given account) and its plan's billing_period_months
    (1/3/6/12, admin's choice — see db.py's BILLING_SCHEMA). Walks forward
    from the anchor in period_months-sized jumps rather than doing date
    math backward from `now`, since calendar months aren't a fixed number
    of days (handles Jan 31 -> Feb 28 etc. correctly via _add_months)."""
    if period_months <= 0:
        period_months = 1
    period_start = anchor
    while True:
        next_start = _add_months(period_start, period_months)
        if next_start > now:
            return period_start
        period_start = next_start


def get_effective_limits(conn, user_id: int) -> dict:
    """Resolve everything an enforcement check needs for *user_id* in one
    call: {"tm_limit": int|None, "seat_limit": int|None, "search_limit":
    int|None, "search_period_start": datetime|None, "features": dict,
    "grace": bool, "grace_until": datetime|None, "blocked": bool}.

    tm_limit/seat_limit/search_limit are None when there's no cap (no
    subscription row — see module docstring). "blocked" is the single flag
    every enforcement call site actually needs to check for "should this
    add be rejected" — it folds together both a real tm_limit being
    at/over capacity concerns (callers still compare counts themselves,
    since this function doesn't know how many the caller is about to add)
    and the expired-past-grace case, so callers don't have to re-derive
    grace logic themselves.
    """
    payer_id = db.resolve_paying_account_id(conn, user_id)
    sub = db.get_subscription(conn, payer_id)

    if sub is None:
        return {
            "tm_limit": None, "seat_limit": None, "search_limit": None, "search_period_start": None,
            "features": {}, "grace": False, "grace_until": None, "blocked": False,
            "plan_name": None,
        }

    plan = db.get_plan(conn, sub["plan_id"])
    tm_limit = sub["tm_limit_override"] if sub["tm_limit_override"] is not None else (plan["tm_limit"] if plan else None)
    seat_limit = sub["seat_limit_override"] if sub["seat_limit_override"] is not None else (plan["seat_limit"] if plan else None)
    search_limit = sub["search_limit_override"] if sub["search_limit_override"] is not None else (plan["search_limit"] if plan else None)
    features = dict(plan["features"] or {}) if plan else {}
    if sub["feature_overrides"]:
        features.update(sub["feature_overrides"])

    grace = False
    grace_until = None
    blocked = sub["status"] == "cancelled"
    renews_at = sub["renews_at"]
    now = datetime.datetime.now(renews_at.tzinfo) if renews_at and renews_at.tzinfo else datetime.datetime.now()
    if renews_at is not None:
        grace_until = renews_at + datetime.timedelta(days=db.SUBSCRIPTION_GRACE_DAYS)
        if now > renews_at:
            grace = now <= grace_until
            blocked = blocked or now > grace_until

    anchor = sub["started_at"]
    period_months = (plan["billing_period_months"] if plan else None) or 1
    search_period_start = _current_period_start(anchor, period_months, now) if anchor else None

    return {
        "tm_limit": tm_limit, "seat_limit": seat_limit,
        "search_limit": search_limit, "search_period_start": search_period_start,
        "features": features,
        "grace": grace, "grace_until": grace_until, "blocked": blocked,
        "plan_name": plan["name"] if plan else None,
    }


def check_can_add_trademarks(conn, user_id: int, count: int) -> tuple[bool, str | None]:
    """Would adding *count* more trademarks push *user_id*'s paying account
    over its limit (or is the account already blocked — see
    get_effective_limits)? Returns (True, None) if the add should proceed,
    (False, reason) otherwise. Call this BEFORE db.add_trademarks_bulk /
    db.add_trademark so a rejected add never reaches the database."""
    payer_id = db.resolve_paying_account_id(conn, user_id)
    limits = get_effective_limits(conn, payer_id)

    if limits["blocked"]:
        return False, "Your subscription has expired. Renew to add more trademarks."

    if limits["tm_limit"] is None:
        return True, None

    current = conn.execute(
        "SELECT count(*) AS n FROM my_trademarks WHERE user_id = %s", (payer_id,),
    ).fetchone()["n"]
    if current + count > limits["tm_limit"]:
        remaining = max(0, limits["tm_limit"] - current)
        return False, (
            f"This would exceed your plan's limit of {limits['tm_limit']} trademarks "
            f"(you have {current}, room for {remaining} more). Upgrade your plan to add more."
        )
    return True, None


def check_can_search(conn, user_id: int) -> tuple[bool, str | None]:
    """Would one more TM Search push *user_id*'s paying account over its
    plan's search_limit for the CURRENT billing period (or is the account
    already blocked)? Only manual TM Search lookups count — see this
    module's docstring for why Auto Discover/Live Refresh/Word Mark don't.
    Call this BEFORE actually running the search (app.py's TM Search route)
    so a rejected search never hits IP India or logs a history row."""
    payer_id = db.resolve_paying_account_id(conn, user_id)
    limits = get_effective_limits(conn, payer_id)

    if limits["blocked"]:
        return False, "Your subscription has expired. Renew to continue searching."
    if limits["search_limit"] is None:
        return True, None

    since = limits["search_period_start"] or datetime.datetime.min
    current = db.count_searches_since(conn, payer_id, since)
    if current + 1 > limits["search_limit"]:
        return False, (
            f"You've used all {limits['search_limit']} TM Searches included in this billing period. "
            f"Upgrade your plan or wait for the next period to search again."
        )
    return True, None


def check_can_add_seat(conn, user_id: int) -> tuple[bool, str | None]:
    """Same shape as check_can_add_trademarks, for adding one more seat
    under *user_id*'s paying account."""
    payer_id = db.resolve_paying_account_id(conn, user_id)
    limits = get_effective_limits(conn, payer_id)

    if limits["blocked"]:
        return False, "Your subscription has expired. Renew to add more seats."
    if limits["seat_limit"] is None:
        return True, None

    current = db.count_seats(conn, payer_id)
    if current + 1 > limits["seat_limit"]:
        return False, f"This would exceed your plan's limit of {limits['seat_limit']} seat(s). Upgrade your plan to add more."
    return True, None


def has_feature(conn, user_id: int, feature: str) -> bool:
    """Feature-gate check for routes like PDF/Excel export, priority
    alerts, white-label — call at the top of any such route. Unlimited
    (no subscription row) accounts get every feature, same "don't break
    pre-existing accounts" rule as the TM/seat limits above."""
    payer_id = db.resolve_paying_account_id(conn, user_id)
    limits = get_effective_limits(conn, payer_id)
    if limits["tm_limit"] is None and limits["seat_limit"] is None and not limits["features"]:
        return True
    return bool(limits["features"].get(feature))
