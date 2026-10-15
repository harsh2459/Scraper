# MarkSimpl vs Our App — Why "Live Refresh" Is Fast There, Slow Here

## Background

MarkSimpl (marksimpl.com) is another trademark-management SaaS. We inspected a
saved browser snapshot of their live site
(`H:\Datascraper\Marksimple\MarkSimpl _ AI-powered Trademark Management.html`
+ its `_files` folder of minified JS/CSS assets) to understand their stack and
how their "Live Refresh" button works, then compared it to our own
`tm_manager` app's equivalent feature.

## MarkSimpl's stack (from the saved bundle)

- **Frontend**: Next.js (routes under `marksimpl.com/_next/static/...`,
  `/app/portfolio`, `/app/trademarks/explorer`, `/app/tmapi/status-check`,
  etc.)
- **Data fetching**: SWR (`useSWR`) for client-side fetch/cache/revalidation
- **Backend API**: hosted on a separate domain, not marksimpl.com itself:
  ```
  API_BASE_URL = "https://api.binbash.ai/api/"
  WS_BASE_URL  = "wss://api.binbash.ai/"
  ```
- Two live-update mechanisms present in the bundle:
  - REST + SWR polling for normal reads
  - A WebSocket connection (`wss://api.binbash.ai/`) defined as a constant,
    presumably for real-time push updates (actual `new WebSocket(...)` call
    site wasn't found in the chunks captured — likely lazy-loaded)

### Their "Live Refresh" button, traced through the bundle

Found the actual handler:

```js
refreshIt: () => E({ id: e, refresh: "true" })
```

and the underlying call:

```js
b({ applicationNumber: u, ...(r ? { refresh: true } : {}) }).unwrap()
```

So:

1. **Normal page load** = cached read — the backend just returns its own
   stored DB record, no external registry call. That's why the initial page
   renders almost instantly.
2. **"Live Refresh" click** = same endpoint, but with `refresh: "true"`
   added — tells the backend "don't serve cache, re-check the source now."
3. It still feels fast because it's a **single, targeted, one-record lookup**
   against the registry (or their own polling backend) — not a bulk scrape,
   and likely not a full authenticated browser session either.

## Our app's stack (`tm_manager`)

- **Frontend**: React + Vite (`tm_manager/frontend`)
- **Backend**: Flask + Postgres (`tm_manager/app.py`, `tm_manager/db.py`)
- **Scraper**: a separate service that drives real authenticated sessions
  against the IP India registry (login + OTP), coordinated via a warm/cold
  account pool

### Our "Live Refresh" flow (`DetailPane.tsx` → `app.py` → `scheduler.py`)

1. Click calls `POST /trademark/<id>/retry`
   ([DetailPane.tsx:253](../tm_manager/frontend/src/components/DetailPane.tsx#L253),
   `app.py:1555` `trademark_retry`) — this just flips the row to
   `status='pending'` and bumps its priority. It does **not** scrape
   anything itself.
2. Frontend then polls `GET /api/trademark/<id>` every **4 seconds**
   ([DetailPane.tsx:148](../tm_manager/frontend/src/components/DetailPane.tsx#L148))
   waiting for the row's status to leave `fetching`/`pending`.
3. A background worker thread only checks for new pending work every
   `_IDLE_POLL_SECONDS = 10s` ([scheduler.py:60](../tm_manager/scheduler.py#L60)).
4. When it picks up work, it doesn't scrape just the one mark — it claims a
   **batch of up to 15 pending trademarks** (`_BATCH_SIZE = 15`,
   [scheduler.py:56](../tm_manager/scheduler.py#L56)) and sends the whole
   batch to the scraper service in one HTTP call.
5. The scraper service does a **real login-driven session** (warm/cold
   account pool, OTP handling) and works through the batch in-process, one
   application at a time.
6. Only `_WORKERS = 2` batches can run concurrently, sharing a **limited pool
   of registry accounts** (OTP email pool) with other scraping features in
   the app — a manual click can land behind other queued work.
7. Worst-case timeout budget is `60 + 15*30 = 510 seconds` per batch
   ([scheduler.py:65](../tm_manager/scheduler.py#L65)) — a single mark can
   legitimately take minutes if it lands in a full batch behind a cold login.

## Root cause of the speed difference

MarkSimpl's registry lookup is almost certainly a **lightweight,
unauthenticated or lightly-authenticated read** against a status endpoint —
cheap, no login, no session state, so a single click completes in roughly
the time of one small HTTP round trip.

Our scrape has to drive a **full authenticated session** (login + OTP,
navigate the actual registry UI) because the IP India portal doesn't expose
a clean API. That session is a shared, rate-limited resource across the
whole fleet, which is exactly why the batching/pooling/queue system exists
in the first place. It's not a bug — it's the tradeoff for scraping a system
that has no real API.

Our own code comment (`scheduler.py:4-12`) already documents part of this
history:

> Calls the scraper's `/api/scrape-appno-batch` — one worker claims a BATCH
> of pending trademarks and hands the whole list to the scraper in a single
> HTTP call... This replaces the old one-HTTP-job-per-mark + 5s-status-poll
> pattern, which paid a claim/release cycle and repeated Flask round-trips
> for every single trademark — that per-mark overhead was the actual reason
> this queue felt much slower than Auto Discover even with the same
> warm-pool accounts underneath both.

## Possible improvement (not yet implemented)

Give "Live Refresh" its own fast lane: for a **manual single-mark refresh**,
skip the batched pending-queue entirely and call the scraper directly for
just that one `app_no` with a dedicated warm account, instead of routing it
through the same batch queue as bulk auto-fetch. This would decouple manual,
user-triggered refreshes (which want low latency) from bulk background
fetching (which wants high throughput and can tolerate queueing).

This has **not been implemented yet** — flagged as a possible next step.

## Follow-up question: "we can't keep someone logged in just for one live fetch"

The concern raised: doing a fast single-mark refresh seems to require a
dedicated login session sitting around, and we don't want to spare a person
or account just to support one manual click.

**No human/manual babysitting is needed — this is exactly what the existing
warm pool already does, automatically.**

`warm_pool.py` runs a background daemon that keeps a set of registry
accounts (`reserved_for = 'warm_pool'`) permanently logged in, so a search
can skip the ~60-90s cold-login cost (CAPTCHA + OTP) and go straight to the
actual search. It re-authenticates itself proactively before sessions expire
([warm_pool.py:19](../warm_pool.py#L19),
`WARM_POOL_MAX_SESSION_AGE_SECONDS` + `_reaper_loop`), does clean logout on
death, and self-heals — zero human involvement.

It already solves the exact sub-problem here — "don't reserve a whole
account just for one live fetch, but also don't make it wait behind a
batch" — via **priority warm slots**
([warm_pool.py:115-121](../warm_pool.py#L115-L121)):

> A priority slot is off-limits to the batch auto-fetch queue... it exists
> so a manual TM Search never has to wait behind a big batch job tying up
> every warm account. A batch of 20 marks can easily hold all 8 warm lanes
> for 10+ minutes; without a reserved lane, a single search started during
> that window falls back to a slow cold login even though it only needs one
> account for a few seconds.

`WARM_POOL_PRIORITY_COUNT` accounts are reserved this way, and
`claim_warm_slot(exclude_priority=True)` is what the batch scheduler calls
so it can never touch those reserved slots — only manual/interactive flows
(like TM Search) can claim them.

### The gap

`AppNoSearch.tsx` / `WordMarkSearch.tsx` (manual TM Search) already use this
priority warm-pool lane. **The "Live Refresh" button on the detail page does
not** — it still goes through `trademark_retry` → `status='pending'` → the
batched `scheduler.py` loop described above, which is the slow path.

### The fix

Route "Live Refresh" the same way manual TM Search already works:

- Add an endpoint, e.g. `POST /api/trademark/<id>/live-refresh`, that calls
  `warm_pool.claim_warm_slot(...)` to grab a priority slot directly, runs the
  single-app-no scrape on that session, updates the row, and releases the
  slot back to `ready` — no queue, no batch, no cold login.
- If no priority slot is free, fall back to the existing cold path (or a
  short "try again" message).

Cost: a small number of dedicated accounts (`WARM_POOL_PRIORITY_COUNT`)
sitting idle-but-logged-in at all times — not a person, just warm sessions —
in exchange for near-instant manual refreshes, similar to what MarkSimpl
appears to offer.

**Status: not yet implemented** — proposed as the next concrete step to close
the speed gap identified above.

## Scraper migration to local machines

**Question raised**: we're moving all scraping off Contabo onto local
machines — how would that work?

**Answer: this is already built**, as a coordinator/worker split
(`FLEET_MODE_ENABLED`, `fleet_api.py`, `fleet_scheduler.py`). It's currently
off by default (opt-in), not yet turned on for a real local machine.

### Roles
- **Contabo (coordinator)**: stays the single source of truth — Postgres,
  the `tm_manager` web app, the browser UI. Runs `fleet_scheduler.py`
  (`FLEET_COORDINATOR_ENABLED=1`). Never scrapes fleet-mode work itself,
  just plans and hands out assignments. Its own live/interactive paths (TM
  Search, Word Mark search, Auto Discover, and the priority warm-pool lane
  proposed above) are untouched by this migration and keep running locally
  on Contabo regardless.
- **Local machines (workers)**: each runs the scraper
  (`app.py` / `ipindia_scraper.py`) with `FLEET_MODE_ENABLED=1` and a unique
  `FLEET_MACHINE_KEY`, pointing `FLEET_COORDINATOR_URL` at Contabo's HTTPS
  URL. They connect to the same shared Postgres.

### Daily flow
1. **Register** — worker calls `POST /api/fleet/register` with its
   `machine_key` once; Contabo upserts it into `fleet_machines`
   ([fleet_api.py:29-41](../tm_manager/fleet_api.py#L29-L41)).
2. **Heartbeat** — worker pings `POST /api/fleet/heartbeat` regularly;
   `FLEET_HEARTBEAT_STALE_SECONDS` marks it dead if it stops.
3. **Daily planning tick** — once a day (past `FLEET_PLAN_HOUR`),
   `fleet_scheduler.py`'s coordinator-only loop calls
   `db.plan_daily_fleet_assignments`, dividing due trademarks across all
   active machines within `FLEET_PER_ACCOUNT_DAILY_CAP` (per-registry-account
   rate limit) and each machine's assigned OTP accounts. A manual "Replan
   now" dashboard button triggers the same function on demand.
4. **Assignment pull** — each worker's local `scheduler.py` polls
   `GET /api/fleet/assignment`, which atomically claims that machine's batch
   of assigned trademarks ([fleet_api.py:64-75](../tm_manager/fleet_api.py#L64-L75))
   plus its pre-assigned OTP accounts (`assigned_machine_id` on
   `otp_email_pool`).
5. **Local execution** — the worker does its own atomic
   `FOR UPDATE SKIP LOCKED` claim locally, then scrapes IP India using its
   own local warm/cold accounts — same `warm_pool.py` /
   `ipindia_scraper.py` logic as today, just running on the local box.
6. **Results flow back** to the shared Postgres, so the web UI on Contabo
   shows updated data regardless of which machine fetched it.

**Status: built but not yet turned on** — needs `FLEET_MODE_ENABLED=1` +
`FLEET_MACHINE_KEY` set on at least one real local machine, and
`FLEET_COORDINATOR_ENABLED=1` on Contabo, to actually go live.

## Subscription-based system

**Requirement**: turn the app into a paid product with 5 tiers (see
MyTMTracker-style pricing — Starter/Pro/Growth/Max + a Custom 5th tier),
priced per year, each with a TM-count limit, a seat limit, and a growing
feature list. One email/password per paying account, which the customer
then manages themselves (seats, usage) under that one login. Needs to be
editable from an admin UI — including per-customer overrides (e.g. bump one
Starter customer from 250 to 300 TMs without changing the plan itself) —
without a redeploy.

### Problem
The app currently has no billing/subscription concept at all — just a flat
`users` table with `is_admin`/`is_active` and per-row `user_id` ownership.
There's no tier, no limit enforcement, no plan editor, no per-account
override mechanism.

### Proposed architecture

**1. Data model** (new tables in the existing Postgres):
- `plans` — id, name (Starter/Pro/Growth/Max/Custom), price, `tm_limit`,
  `seat_limit`, feature flags (JSONB, e.g.
  `{pdf_export: true, priority_alerts: true, white_label: false, ...}`),
  billing period.
- `subscriptions` — `user_id` (account owner) → `plan_id`, status
  (active/expired/cancelled), `started_at`, `renews_at`/`expires_at`,
  `tm_limit_override` (nullable — null = use plan default),
  `seat_limit_override` (nullable), `feature_overrides` (nullable JSONB,
  merges over plan defaults). This is what lets an admin give one customer
  300 instead of the plan's 250 without touching the Starter plan itself.
- `account_members` (or `users.parent_account_id`) — the seats under one
  paying account, matching "one email/password, they manage it themselves."

**2. Enforcement hooks** in the existing app — a small `billing.py` with
`get_effective_limits(user_id)` (resolves plan defaults + overrides once),
checked at: adding a trademark (block over `tm_limit`), inviting a seat
(block over `seat_limit`), and any feature-gated route (check
`feature_overrides`/plan flags, e.g. white-label, PDF export, priority
alerts).

**3. Admin backend + frontend** — a new, separate service (mirrors the
existing isolation between `app.py` scraper, `tm_manager` customer app, and
`pdf_scraper`), reusing the same Postgres and the same admin-auth pattern
already used by the fleet dashboard (`require_admin()`). Lets you: list
customers, see live usage (e.g. "180/250 TMs used"), edit overrides inline,
and create/edit the plan definitions themselves (price, limits, feature
flags) without a redeploy.

**4. Payment processor** — deliberately deferred. The architecture above
works today with admin-assigned subscriptions (flip a customer active
manually); Stripe/Razorpay webhooks can be added later to automate
`subscriptions.status` / `renews_at` by calling the same functions the admin
UI already calls — no rework needed.

**Status: planned, not yet implemented.**

## Making everything fast

**Requirement**: make the whole system (scraping + the new subscription
system) as fast as possible.

### Problems found in the current stack
- No caching layer anywhere — `db.py` has zero references to
  `cache`/`redis`; every page load and every poll hits Postgres cold.
- Production server may be running Flask's built-in dev server
  (`app.run(..., threaded=True)`) instead of Waitress — [app.py:1925-1932](../tm_manager/app.py#L1925-L1932)
  only uses Waitress if `USE_WAITRESS=1` is explicitly set; Flask's own
  server isn't built for real production concurrency even threaded.
- Several frontend views poll on fixed intervals (4s in `DetailPane.tsx`,
  10s elsewhere) — fine per user today, but N concurrent users means N×
  redundant polling load once the subscription system brings more
  concurrent customers.
- "Live Refresh" itself is slow for the reasons already covered above
  (batch queue instead of a dedicated fast lane) — the single biggest
  user-perceived speed problem today.

### Solutions, in priority order
1. **Priority warm-pool lane for Live Refresh** (see above) — biggest,
   cheapest win for user-perceived speed on the one feature already flagged
   as slow. Do this first.
2. **Confirm Waitress is actually on in production** (`USE_WAITRESS=1`) —
   near-zero-risk, immediately-felt concurrency fix. Consider multi-worker
   (gunicorn-style) for the new admin/billing service too.
3. **Add a caching layer (Redis, or in-process cache to start)** once the
   subscription system has real read traffic to cache — cache
   `get_effective_limits(user_id)` (checked on nearly every write) and
   portfolio/dashboard aggregate counts, so repeated polls hit memory
   instead of Postgres.
4. **Index every new billing table's hot lookup columns** from day one
   (`subscriptions.user_id`, `account_members.parent_account_id`, etc.) —
   same discipline already used for `my_trademarks`
   (`idx_my_trademarks_queue`, `idx_my_trademarks_status`).
5. **Reduce redundant frontend polling** — longer term, replace fixed-interval
   polling with WebSocket/SSE push (same pattern MarkSimpl's bundle hinted
   at with `WS_BASE_URL`) so more concurrent users doesn't mean
   proportionally more polling load. Bigger lift — do this only after the
   simpler wins above.
6. **Keep the admin/billing service isolated from the scraper and customer
   app** — same process-isolation principle already used between `app.py`
   (scraper) and `tm_manager` (customer app), so a heavy admin query can
   never slow down a customer's live TM lookup.

**Recommended order of attack**: #1 and #2 first (near-zero risk, immediate
impact), #3 once the subscription system exists and has real traffic to
cache, #4 built in from the start of the billing work (not retrofitted),
#5/#6 designed for now but implemented later.

## Production/SaaS readiness gaps

Beyond the items above (warm-pool priority lane, fleet migration,
subscriptions, caching, Waitress), these are still missing for this to be a
real multi-tenant paid product:

1. **No rate limiting anywhere** — nothing guards login, signup, or the API.
   Directly risks the shared warm-pool/OTP account pool if abused.
2. **No automated backups** — `pg_dump` exists only as a manual command in
   deploy docs ([deploy/HOW-TO-UPDATE.md:186](../deploy/HOW-TO-UPDATE.md#L186),
   [deploy/LAUNCH.md:93](../deploy/LAUNCH.md#L93)), nothing scheduled.
   Non-negotiable before charging real customers.
3. **No error tracking / observability** — errors only go to local logs
   ([tm_manager/app.py:50](../tm_manager/app.py#L50)); no Sentry, no alerting,
   no uptime monitoring tied to the fleet heartbeat that already exists.
4. **Multi-tenant data isolation needs an explicit audit** — ownership is
   enforced via scattered `WHERE user_id = ...` checks in `db.py`; once
   `account_members`/seats land, one missed check is a cross-tenant data
   leak, not just a bug.
5. **No self-serve signup flow** — `create_user` exists but there's no
   public signup → email verification → plan selection → payment path.
6. **No Terms of Service / Privacy Policy / data retention policy** — real
   compliance surface given client PII (owner/attorney addresses) is stored.
7. **No real secrets management story at fleet scale** — `.env` per machine
   is fine for one server; N fleet workers each holding `FLEET_MACHINE_KEY`
   + OTP credentials need a rotation/compromise plan.
8. **No cross-fleet request pacing against the registry** — more paying
   customers means more concurrent scraping; without fleet-wide pacing,
   growth risks IP India tightening CAPTCHA/blocking in response to load.
9. **No plan-downgrade/dunning policy** — undefined behavior when a
   subscription lapses or a customer downgrades below current usage (e.g.
   Pro→Starter with 400 TMs against a 250 cap). Product decision, not just
   code.
10. **Hardcoded, stale User-Agent string** — every scrape request from every
    account, every session, sends the exact same
    `Chrome/124.0` UA ([ipindia_scraper.py:51-54](../ipindia_scraper.py#L51)).
    Chrome 124 is now 18+ months old. A real browser auto-updates; a fleet of
    N machines all sending an identical ancient UA string is a textbook
    bot-detection signal. As the fleet grows and request volume increases,
    this becomes the easiest handle for IP India's WAF to grab. Fix: rotate
    UA per session using `fake-useragent` (see concrete plan below) and add
    the missing browser headers (`Accept-Language`, `Sec-Ch-Ua`,
    `Sec-Fetch-Site`) that real Chrome sends alongside its UA.

**Priority read**: #1 and #3 are cheap, do them alongside the subscription
work (both hook into the same `before_request`/`g.user_id` layer). #2 is the
one flagged as "don't skip" — non-negotiable before charging real customers.
#10 (UA rotation) is a 30-minute, zero-risk change that should be done
immediately — it costs nothing and removes the single easiest bot-detection
signal before fleet expansion. #4-9 matter but can follow once the
subscription system ships.

### Rate limiting — concrete plan

**Library**: `Flask-Limiter` — fits the existing `before_request`/`g.user_id`
pattern ([tm_manager/app.py:196](../tm_manager/app.py#L196)) directly, and
supports Redis as a backend (which the caching plan above already calls
for, so it's reusing infra, not adding new infra).

Where to apply it:
1. **Login** (`/api/login`, [tm_manager/app.py:114](../tm_manager/app.py#L114))
   — tightest limit, e.g. 5 attempts/minute per IP. The only unauthenticated
   write endpoint and the obvious brute-force target.
2. **Global default** — a generous per-user limit, keyed on `g.user_id` once
   known (falling back to IP before login), covering every route.
3. **Scraper-triggering endpoints specifically** — `/trademark/<id>/retry`,
   `/api/trademark/<id>/fetch-now`, bulk fetch-all — these cost a shared
   OTP/warm-pool account per call, so they need their own tighter limit
   independent of the general API limit. This is also where **tier-based
   limits** plug in later: Starter gets fewer manual refreshes/day than Max,
   enforced as a Flask-Limiter rule keyed on the customer's plan.

Shape: one `Limiter(app, key_func=lambda: getattr(g, "user_id", None) or
get_remote_address(), storage_uri="redis://...")` initialized near the
existing `CORS(app, ...)` call ([tm_manager/app.py:57](../tm_manager/app.py#L57)),
then `@limiter.limit(...)` decorators on the routes above. Keying by
`g.user_id` (resolved by the existing `before_request` before most routes
run) means one abusive account can't hide behind IP-based limits.

**Status: not yet implemented.**

### Error tracking — concrete plan

**Tool**: Sentry (`sentry-sdk[flask]`) — free tier to start, first-class
Flask integration (a few lines to init), separates errors by project so
scraper errors, tm_manager errors, and pdf_scraper errors don't all land in
one undifferentiated log file.

Where to wire it in:
1. One `sentry_sdk.init(dsn=..., traces_sample_rate=0.1)` call per service —
   `tm_manager/app.py`, `app.py` (scraper), `pdf_scraper/app.py` — each with
   its own DSN/project, so alerts are pre-segmented by service.
2. Tag every event with `user_id` (from `g.user_id`) so a customer's report
   can be searched by their account instead of guessing.
3. Tag scraper-side errors with `app_no`/`machine_id` — the retryable-error
   list already in `scheduler.py` (OTP timeouts, "already logged in on
   another tab," etc.) becomes structured, queryable data instead of scroll-
   through-logs, turning "the scraper is flaky sometimes" into "these 3
   error types happen at this rate, mostly on these 2 fleet machines."
4. Set up **alerting** (Sentry → email/Slack) for new error types and
   error-rate spikes — the actual point: find out about a broken paid
   feature before the customer emails about it, not after.

**Status: not yet implemented.**

**Status: planned, not yet implemented.**

### User-Agent rotation — concrete plan

**The problem**: `_USER_AGENT` at
[ipindia_scraper.py:51](../ipindia_scraper.py#L51) is a single hardcoded
string applied to every session by `_make_session()`
([ipindia_scraper.py:227](../ipindia_scraper.py#L227)). It hasn't been
updated since Chrome 124 (April 2024). Every account, every login, every
HTTP call to IP India uses the exact same fingerprint.

**Library**: `fake-useragent` — lightweight, zero-config, auto-updates its
UA database from the web. Falls back to a hardcoded string if the download
fails (airgapped envs, first run before internet access).

**Where to apply it**: at `_make_session()` only — per session, not per
request. A real browser doesn't change its UA mid-session; doing so would be
more detectable, not less.

```python
# ipindia_scraper.py — replace the hardcoded _USER_AGENT block
try:
    from fake_useragent import UserAgent as _UA
    _ua_gen = _UA(
        browsers=["chrome", "firefox", "edge"],
        os=["windows", "macos"],
        fallback=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    )
    def _random_ua() -> str:
        return _ua_gen.random
except Exception:
    def _random_ua() -> str:  # graceful fallback — library unavailable
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )


def _make_session() -> requests.Session:
    sess = requests.Session()
    ua = _random_ua()
    sess.headers.update({
        "User-Agent": ua,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://tmrsearch.ipindia.gov.in",
        "Referer": f"{BASE}/OTP/index",
        # Sec-Ch-Ua omitted intentionally — only valid for Chromium-family UAs
        # and would be absent for Firefox, creating a mismatch if Firefox UA
        # is picked. Better to omit than to send a mismatched signal.
    })
    ...
```

**Add to `requirements.txt`**:
```
fake-useragent>=1.5
```

What this changes:
- Every new `IPIndiaSession` (i.e. every warm-pool slot login, every cold
  login, every fast-fetch thread) gets its own random, current-version UA.
- Warm-pool slots that have been running for hours will keep their original
  UA for the life of that session (correct — matches real browser behaviour).
- No change to any other scraper logic; `_make_session()` is the only
  touch point.
- `Accept-Language` and `Accept-Encoding` headers are added alongside —
  these are normally present in every real browser request and their
  absence is itself a detectable signal.

**Rotation strategy**: per-session (not per-request) is the correct approach.
Rotating UA on every request within a session is actively worse — real
browsers never do this and IP India's server can detect the inconsistency
between cookies (which persist across a session) and a changing UA.

**Status: not yet implemented** — low-effort, recommend doing alongside
any other `ipindia_scraper.py` changes (e.g. the Live Refresh fast-lane
work) rather than as a standalone PR.
