# Datascraper update implementation plan

## Scope and working rules

Implement the redesign in `H:\Datascraper-updates`, which the user identified as a copy of the application. The user subsequently approved implementation. The first runnable fleet release is now implemented and fixture-tested in this copy; live deployment has not started. See [FLEET-RUNBOOK.md](FLEET-RUNBOOK.md) for configuration, verification, staging steps and remaining rollout work.

Do not assume the copy is isolated from production services: existing `.env` files may still point at Contabo PostgreSQL, Drive, email accounts or monitoring. Development and tests must use dedicated local configuration, synthetic fixtures and an isolated database. Do not run current app entrypoints merely to inspect them: they start background workers and perform database changes.

This plan supersedes the initial architecture where it differs. Preserve working portal authentication and existing user data/document access. The subsequent approved correctness work improves PDF parsing and replaces status-only skipping alongside the new orchestration.

Saved portal formats have now been inspected directly in `D:\tmsearch`. See [PORTAL-SNAPSHOT-AUDIT.md](PORTAL-SNAPSHOT-AUDIT.md) for verified request payloads, opposition/notice table layouts, metadata fixes and the next data-correctness workstream. The source `OPPOSITION-AND-COMPLIANCE-FLOW.md` extends this plan; its historical measurements and legal-date examples are not current live verification.

## Route performance and claim ordering correction

The review is complete: Windows route selection now runs in-process with a two-second cache, invalidated on config/destination/lifecycle changes and failures. Local configuration/DNS preflight precedes new scraping claims, so those failures consume no attempts. Repair-only claims preserve saved-PDF processing when VPN preflight is broken. Deploy the coordinator before these workers; this additive claim filter needs no migration. See the correction section in [VPN-ROUTING-PLAN.md](VPN-ROUTING-PLAN.md) for measured native-route timings and remaining post-preflight failure behavior.

## Target architecture

| Component | Location | Responsibility |
|---|---|---|
| User backend | Contabo; existing `shared-scraper/tm_manager/` | Authentication, users, subscriptions, portfolios, user refresh requests and reading accepted results |
| Scraper management backend | Contabo; new `shared-scraper/control_plane/` | Refresh policy, durable jobs, fleet identity, offline reservations, result acceptance, artifacts and operational administration |
| User frontend | Contabo; existing `tm_manager/frontend/` | Portfolio and trademark experience |
| Administration frontend | Contabo; new `shared-scraper/admin_frontend/` | Users/subscriptions plus schedules/jobs/fleet/results/alerts; calls each owning backend |
| Worker agent and local API | Worker PC; new `shared-scraper/worker_agent/` | Local assignments, scraping cycle, VPN lifecycle, SQLite outbox, delivery and local controls |
| Worker frontend | Worker PC; new `shared-scraper/worker_frontend/` | Worker status, assignments, VPN, local results, uploads and diagnostics |
| PDF processing | Worker PC; existing `pdf_scraper/` adapted | Local extraction without Contabo database access |

Both Contabo backends use one PostgreSQL database with distinct service roles and explicit table ownership. Workers use HTTPS APIs and local SQLite; remove their need for production PostgreSQL credentials after cutover.

The admin frontend manages user/subscription records through the user backend, and scraper operations through the control backend. A shared database does not grant either backend permission to modify all tables arbitrarily. Administrative navigation moves out of the portfolio UI after equivalent controls are verified.

Initial stack: retain Python/Flask backend code and React/TypeScript UI components. Use PostgreSQL as the source of truth for jobs, reservations and `next_fetch_at`. BullMQ has been discussed but not selected. Keep dispatch behind an interface so a BullMQ service can be introduced on Contabo later without changing the worker protocol. If BullMQ is selected, add a transactional database outbox and reconciliation so database writes and queue publications cannot diverge; fleet PCs still use HTTPS rather than directly consuming queue jobs during VPN disconnection.

## VPN routing refinement

All 21 copied WireGuard configs currently allow only `141.148.213.237/32`. They describe destination-based split routing, so Contabo HTTPS may remain reachable during scraping. This routes every process accessing that IP through the tunnel; it does not isolate only the scraper process. All configs also specify `10.2.0.1` as DNS; eight additionally specify `2a07:b944::2:1`. Neither DNS address is covered by the configured allowed range. DNS reachability, current portal addresses and coordinator connectivity through an active tunnel remain unverified.

The routing refinement is now implemented in this copy. `WORKER_NETWORK_MODE=offline` remains the default bounded scrape/disconnect/upload cycle. Optional `split` mode verifies portal/DNS routes and coordinator HTTPS, sends periodic heartbeats, and uploads saved results during scraping. Coordinator outages retain the outbox and existing reservation budget; portal routing failures block requests. Both modes retain disconnect/reconcile recovery. Portal HTTPS uses vetted IPs with original-hostname TLS validation; proxies and unreviewed redirects are blocked in the worker path. Detailed configuration, the read-only audit command, per-worker provisioning and remaining live pilot checks are in [VPN-ROUTING-PLAN.md](VPN-ROUTING-PLAN.md).

Workers continue using the coordinator HTTPS API, never production PostgreSQL. A test of Contabo port 5432 is not an acceptance test for the new transport. VPN routing does not change the PostgreSQL queue choice or the separation between the two backends.

Live pilot preparation has now been executed: a separate config with ordinary machine DNS and the current portal `/32` route passes real preflight. Its directory has a protected user/SYSTEM/Administrators ACL; the 21 original exports are unchanged. `WORKER_VPN_CONFIG_DIR` supports this separate private directory. The reproducible `deploy/Invoke-WorkerVpnPilot.ps1` helper includes baseline/connected/post-cleanup checks and owned-tunnel cleanup. The connected test is awaiting the actual staging coordinator HTTPS URL and a Windows administrator session; no tunnel has been started. See the execution-status section of `VPN-ROUTING-PLAN.md` and the key-free `.runtime/vpn-pilot/pilot-report.json`.

## Core data and lifecycle

Introduce additive tables, with final names adapted to the existing schema during phase 1:

| Table/domain | Owner | Purpose |
|---|---|---|
| Users/subscriptions/portfolio membership | User backend | Private ownership, access and entitlements |
| Canonical trademarks | Control backend | One public application identity shared by portfolios |
| Fetch requests | User backend writes; control backend consumes | Durable handoff committed with the originating user operation |
| Refresh policies and schedules | Control backend | Last successful observation, next fetch, overrides and policy version |
| Jobs and attempts | Control backend | Priority, generation, assignment, retry state and outcomes |
| Workers and portal-account assignments | Control backend | Individual identities, capacity, operational state and credentials |
| Accepted observations and artifact metadata | Control backend | Immutable results, checksums, document references and change history |
| Local assignments/results/outbox | Worker agent | Recovery and delivery while disconnected |

Only the due schedules enter the active work queue; long-term future dates remain indexed database schedule records. Successful result acceptance computes the next fetch. A failed scrape or pending delivery does not advance it.

Separate these facts: portal fetch outcome, PDF extraction outcome, delivery outcome, and portfolio presentation. A local scrape is not server-complete until required data/artifacts are acknowledged.

Contabo job lifecycle: `queued -> reserved -> completed`, with `retry_wait`, `failed` and `cancelled` paths. Worker lifecycle: `received -> scraping -> result_saved -> upload_pending -> acknowledged`, with persistent partial/error state. Reservations identify the worker and generation and expire after a bounded offline batch plus reconnect allowance.

## Phase 1 — Isolate development and define contracts

Deliverables:

- Inventory runtime imports and all direct DB writes in scraper, PDF processor, scheduler and offline modules.
- Add safe development configuration templates for both backends and workers; production integrations disabled in tests.
- Define versioned assignment/result manifests, error classifications and completion criteria for required artifacts.
- Specify service table ownership, API access, reservation duration/grace policy and timestamp semantics.
- Establish controlled local PostgreSQL and synthetic portal/PDF fixtures.

Acceptance: inspection and tests cannot contact production PostgreSQL, send email, fetch live portal data or upload production documents. Contracts account for a worker being unable to contact Contabo for its entire VPN batch.

## Phase 2 — Database migrations and durable requests

Deliverables:

- Add a versioned migration runner under `shared-scraper/migrations/`, rather than depending on importing application code to migrate.
- Add canonical identity, request outbox, policy, schedule, job/attempt, result/artifact and worker reservation fields/tables.
- Backfill canonical identities from existing portfolio/application records without merging user-private notes or ownership.
- Add indexes for due schedules, available jobs and result identity; enforce one open refresh generation per application.
- Provide least-privilege service roles and read access/views for accepted data.
- Write portfolio additions and their fetch requests in one transaction. Assign a stable request ID for consumption retries.

Acceptance: migration rehearsal on a disposable copy preserves portfolios and results; repeated request consumption creates no duplicate work. Shared applications can update multiple portfolios correctly.

## Phase 3 — Contabo scraper management backend

Deliverables in `control_plane/`:

- Configuration, database repositories, authentication, request validation and structured audit events.
- Scheduling policy engine and bounded, indexed due-work dispatcher.
- Atomic claim/reservation, pause/drain behavior, expected-offline state, expiration and reassignment.
- Individually revocable worker authentication scoped to that worker's jobs; operator/admin authorization for management APIs.
- Versioned worker endpoints for configuration, reconciliation, claim, offline reservation acknowledgment, upload, completion and failure.
- Idempotent result acceptance, observation-time ordering, validation, artifact checksum checks and history updates.
- Store observation publication and job completion consistently in PostgreSQL. Artifact uploads use staging and become referenced only after verification; clean orphaned uploads separately.
- Metrics for queue age, overdue schedules, reservations, pending delivery, retries and completion throughput.

Acceptance: concurrent claims do not grant one job to two valid owners; duplicate delivery returns prior acceptance; expired/stale authority cannot overwrite newer data. Midnight and coordinator restart do not strand work.

## Phase 4 — Durable local worker and VPN cycle

Deliverables in `worker_agent/` and adapted existing scraper modules:

- Local SQLite store, atomic artifact writes, checksummed manifests, storage thresholds and restart recovery.
- Result sink abstraction: parsing/fetching returns data into a local result bundle instead of writing Contabo rows during the new mode.
- PDF extraction path returns fields/artifacts locally without remote database dependencies.
- Worker first reconciles/uploads saved results, then downloads and persists a bounded batch and its offline reservation.
- Connect VPN, verify portal, scrape saved assignments, checkpoint every item, disconnect VPN and verify Contabo access before delivery.
- Cache reuse checks application identity, requested generation/freshness and completeness. Retry extraction/upload without repeating an already successful portal fetch.
- Backoff and portal-account cooldowns; expected offline timing, finite batch limits, pause/drain and graceful shutdown.
- Local API binds to loopback; local control requests require authorization and protection against browser-origin misuse. Redact credentials in diagnostics.

Adapt `app.py`, `pdf_scraper/pdf_processor.py`, `offline_queue.py`, `pending_sync.py` and `vpn_rotator.py` through explicit new-mode interfaces rather than silently changing all legacy callers at once.

Acceptance: stop/crash/reboot at each lifecycle boundary preserves acknowledged assignments and saved results. Network/VPN outages retain undelivered work. Lost completion responses cause safe delivery retries. Workers can scrape a downloaded batch without Contabo DB connectivity.

## Phase 5 — Route TM Manager through the new workflow

Deliverables:

- Change addition/import/manual-refresh routes to durable fetch requests; stop starting `_run_fast_fetch_one` on Contabo in the new mode.
- Remove competing fleet scheduling, global fetching reset and direct scraper calls from the user backend when the new dispatcher is enabled.
- Replace fixed global refresh intervals with per-application schedules owned by the control backend.
- Preserve last successful data while queued/running; expose observation date, next fetch, pending state and error state to users.
- Read accepted results/documents through shared read models or control-backend APIs with portfolio permissions enforced by the user backend.
- Support one canonical application across portfolios and calculate an effective shared refresh policy explicitly. The earliest authorized required refresh can govern the public observation; subscription rules still control requests and private access.
- Review remaining search/watch/journal features: route portal-dependent work through the new job protocol before disabling the old live scraper. Keep journal parsing/matching as a distinct server task where suitable.

Acceptance: adding a TM or requesting an update produces fleet work even when no Contabo scraper is running. User isolation, subscriptions, imports, document viewing and status history remain functional.

## Phase 6 — Contabo administration frontend

Deliverables in `admin_frontend/`:

- Reuse suitable components from `ManageUsers`, `ManagePlans`, `ManageSubscriptions` and `Fleet`.
- Build overview, users/subscriptions, canonical TM inventory, schedule policies/overrides, jobs, fleet/accounts, results/artifacts, alerts and audit screens.
- Show expected VPN-offline workers separately from overdue/unreachable workers.
- Provide operator actions for pause/drain, retry, priority changes, schedule overrides and reservation recovery with audit records.
- Route backend requests through same-origin nginx paths. Establish server-validated admin authentication across both backends, with compatible session validation or scoped service authorization rather than relying on frontend route guards.
- Remove migrated administrative navigation from the portfolio frontend after parity is verified.

Acceptance: an authorized administrator can manage users and operations from this panel; an ordinary user cannot access its APIs. Lists are paginated and remain usable with large inventories.

## Phase 7 — Local worker frontend

Deliverables in `worker_frontend/`:

- Overview with current phase, VPN/internet/portal/Contabo checks and batch progress.
- Assignment list and local results with distinct scrape/extraction/upload states.
- VPN controls, saved server/configuration status, cooldown/error display.
- Delivery queue, artifact availability, retry-upload/retry-extraction actions and storage health.
- Pause/drain/resume, readable diagnostics and redacted configuration.

Acceptance: the UI works without internet because assets and APIs are local. An operator can distinguish unfinished scraping from finished-but-undelivered work and recover delivery without rescraping.

## Phase 8 — Verification, deployment preparation and migration

Required integration scenarios:

1. Two workers claim concurrently; application shared by multiple users; large import alongside urgent refresh.
2. Worker loses internet before/during scraping; VPN connect/disconnect fails; Contabo is down after scraping.
3. Crash after assignment persistence, artifact write, upload and server acceptance; restart one worker while another is active.
4. Reservation expiration, late result after reassignment, repeated completion, mismatched/checksum-failed artifact and stale cache.
5. Midnight rollover, overdue work, schedule changes, retry exhaustion and PDF-only extraction retry.
6. Worker revocation/pause/drain, user authorization, subscription limits and admin API authorization.
7. End-to-end user addition -> fleet fetch -> artifact delivery -> accepted history -> visible portfolio update.

Deployment deliverables:

- New systemd services, nginx routing, environment templates and corrected Windows launch scripts.
- Development launcher uses the actual `shared-scraper` paths and includes the worker/PDF/UI processes appropriate to each role.
- Separate coordinator and worker configuration; worker software never starts a user backend as a side effect.
- Rehearse backup/restore and additive migrations; document toggles and cutover steps.
- Keep exactly one dispatch mode active. Drain old work and reconcile saved worker results before switching ownership.
- Pilot one worker, then two, then 5-6. Observe actual throughput and choose batch/reservation sizes from measurements.
- On rollback, pause/drain the new queue and reconcile results before enabling old dispatch. Do not blindly start both schedulers or drop new tables.

Live deployment is a separate step; preparing this copied workspace does not deploy changes to Contabo.

## Decisions to keep configurable

- Refresh policy intervals/status rules, including the meaning of the proposed ten-year interval. No legal deadline or monitoring interval is inferred automatically.
- Required fields/artifacts for full completion and treatment of partial results.
- Offline batch duration, grace time, retry limits and local/server retention.
- Existing Drive storage versus Contabo-managed document storage; choose one verified delivery contract before rollout.
- Python/PostgreSQL dispatch baseline versus introducing BullMQ on Contabo. Preserve the same durable request/result protocol either way.

## First implementation milestone

Demonstrate one user adding one TM, a worker receiving it without production DB access, scraping a fixture through a simulated offline cycle, saving locally, reconnecting, uploading once or repeatedly, and the user seeing one accepted update with working artifacts.

This proves the lifecycle before expanding policies, fleet scale or management screens.

## Follow-on: opposition, compliance and document correctness

Implement this alongside the existing phases using the saved-format audit above:

1. **Contracts and fixtures (Phase 1):** sanitized opposition/notice fixtures; per-stage/per-document outcomes, source/proceeding identity and parser version. Keep scrape time distinct from portal as-on date. Define partial/review-needed states instead of using only class/date as proof of every stage's completion.
2. **Storage (Phase 2):** public proceeding/notice/document ledger and source-linked event tables; private receipt dates, confirmations and task statuses owned by the user backend. Version approved deadline rules and represent periods by value/unit. Preserve existing source IDs during migration so completed tasks do not reappear as new.
3. **Fetching and extraction (Phase 4):** discover and follow fresh opposition links; always inspect lightweight change sources, download only missing/changed documents, retain independent retry states and allow local PDF re-extraction. Use stable identity/meaningful metadata fingerprints, periodic full checks and explicit forced-full controls. Harden field extraction/merging without hiding explicit blank values or newer corrections.
4. **User workflow (Phase 5):** Tasks, trademark details and digest read source-linked events with SQL date-window pagination and portfolio party/side filtering. Unknown dates appear for review. Show source documents, deadline basis, receipt date and confirmation history. Do not turn an acceptance letter into a hearing task based on its title.
5. **Management/UI (Phases 6-7):** stage-aware refresh policy and document coverage/review screens; then week view, compliance/tag filters, renewal tasks and exports. Richer reminders/calendar feeds follow verified event semantics.
6. **Verification (Phase 8):** nested/header variants, opaque/root-relative links, multiple proceedings, duplicate descriptions, metadata edits under one ID, empty/error list responses, scans/multi-class forms, field provenance, repeat publication with preserved task status, role separation, large-portfolio pagination and calendar-month arithmetic. Require real extra samples for decisions/rectifications/hearing layouts before claiming their parser coverage.

## Correctness implementation status (2026-10-03)

The six gaps identified in the review are now implemented in the copied fleet path:

| Gap | Implemented behavior |
|---|---|
| Opposition fetching/storage | Follow fresh same-origin proceeding/detail/reminder links, download notices, persist separate proceeding identities and source-linked events. Outgoing tasks require a private, explicit proceeding membership. |
| Compliance dates | Read document facts; acceptance text overrides misleading hearing titles. Hearing dates require a stated hearing date. Receipt-based periods remain unresolved until receipt is entered; months use calendar arithmetic. Private confirmation is bound to the source version and becomes stale when that version changes. SQL date windows and pagination replace the 1,000-row compliance limit. |
| PDF extraction | Independent class/use parsing, multiple classes, forward/reversed/multiline labels, all-page bounded extraction, missing-versus-blank handling, complete field projection, provenance and optional bounded local Tesseract OCR. Scans without OCR/unrecognized layouts remain explicitly marked for review. |
| Fetch depth | Always check small lists/details/proceedings; compare stable namespaces and meaningful metadata. Reuse checksummed local bytes, retry missing downloads, reparse cached bytes, refresh ancillary panels when fingerprints change, enforce configurable safety checks, and expose forced full downloads. Legacy status-only skipping is disabled. |
| Extra document rows | Upsert extracted content by application/checksum and maintain one merged canonical read model with per-field source artifacts. Repeated results remain immutable history. Historical legacy rows are retained; no destructive cleanup is performed. |
| Strict partial publication | Publish verified core fields and successful documents while required missing stages remain queued for retry. Optional failures/review-needed extraction do not suppress valid core data. Failed/partial fetches do not advance the successful schedule. |

Migrations `005`–`012` add the document/event ledger, private preferences/memberships/audit, canonical document identity, legacy review/provenance backfill, PDF repair assignments and source-bound confirmations. Reapply the service grants after migration. PDF-only repair creates a new reservation/result, uses saved artifacts without portal/VPN access, and preserves the original portal observation time and next-fetch schedule.

User screens include proceeding records, document coverage/source links, compliance review, receipt entry, confirmation, private notes, side/type filters and explicit event pagination. Administration includes document coverage, proceedings and compliance facts; the local worker exposes retry extraction from saved files.

Verification: **96 automated tests pass**, including the routing refinement, private config-directory support, native Windows routes/cache behavior and preflight attempt protection, and the management frontends build and pass TypeScript checks. Tests include real synthetic PDFs, saved/sanitized portal layouts, incremental downloads, checksum damage, metadata changes, partial publication, private receipt isolation, changed-source confirmation invalidation, 1,205-event pagination, a repair cycle that rejects all VPN/portal calls, split/offline transport, DNS/IPv6/route failures, pinned-IP TLS hostname validation and monitor teardown recovery. The unchanged user frontend was built during the correctness release. The hidden-browser fixture review passes for the new connectivity screens and existing compliance flow.

The isolated demo was migrated/restarted with the current code. A hidden-browser review also passes for the new administration pages, receipt entry/calendar-month calculation, private deadline confirmation and worker results; review screenshots are saved in `.runtime/correctness-*.png`.

The optional parity/enhancement items above (week view, tags, exports, calendar feeds, richer alert delivery and rule administration) are separate from these six data-correctness fixes. Live portal/VPN deployment, unknown decision/rectification layouts and actual scan accuracy require staging samples/pilot verification; the copy has not been deployed to Contabo.
