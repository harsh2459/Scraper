# Contabo control panel and local scraping workers

Status: requirements and proposed design; application changes are not implemented by this document.

Implementation roadmap: `IMPLEMENTATION-PLAN.md`. The agreed separation is two backends on Contabo sharing PostgreSQL, with a dedicated administration frontend and a local worker frontend.

Subsequent routing inspection found that the 21 copied VPN configs use a portal-only destination route. The offline cycle in section 5 remains the default recovery contract; Contabo becoming unreachable is a condition to handle, rather than an assumption about every VPN. [VPN-ROUTING-PLAN.md](VPN-ROUTING-PLAN.md) defines the implemented optional online transport, isolated verification and remaining live pilot checks. Local persistence and server-controlled acceptance are required in either mode.

## Ownership and interfaces

| Interface | Responsibilities |
|---|---|
| Contabo control panel | Users, subscriptions, global trademark inventory, refresh policies, jobs, fleet, portal accounts, result acceptance, storage, operational alerts and audit history |
| TM Manager | User login, the user's portfolios, adding trademarks, requesting refreshes, viewing data/documents/changes and subscription usage |
| Local worker UI | Local assignments, VPN controls, scraping/PDF progress, saved results, uploads, connectivity, retry controls and local storage health |

Administrative user, subscription, fleet and scraper management moves out of TM Manager. TM Manager continues consuming Contabo APIs and enforcing user permissions server-side. Moving controls out of the UI must not remove existing authorization checks or subscription enforcement.

## 1. Contabo user and subscription management

- Create, view, update, deactivate and reactivate users; reset passwords and revoke sessions; manage administrator/operator/user permissions.
- Manage plans, subscription periods/status, portfolio limits and manual-refresh allowances. Show usage and enforce limits in backend APIs.
- Keep portfolio ownership and private notes separate from the shared public trademark data.
- Audit administrative changes. Account deletion needs explicit retention and portfolio cleanup behavior; disabling an account is the ordinary reversible operation.

## 2. Fleet management

- Register any number of workers with unique identities and individually revocable credentials.
- Assign portal accounts to workers without sharing one active portal account across simultaneous workers.
- Show last contact, expected VPN offline period, current batch, last successful upload, queued local results, recent errors, software version and capacity.
- Support pause, drain, resume, credential revocation and retirement. Draining stops new assignments while allowing saved results to upload.
- Distinguish idle, reserved, scraping through VPN, awaiting upload, syncing, paused and unexpectedly unreachable workers.
- Treat capacity as a measured limit. Add new machines through registration/configuration, without code changes.

## 3. Trademark scheduling

Keep one canonical trademark record per application identity and separate portfolio membership records for users. Multiple users tracking the same trademark share a refresh result rather than scheduling duplicate scrapes.

Store at least:

- `last_successful_fetch_at`, `next_fetch_at`, `schedule_policy_id`, schedule override and its reason;
- last observed portal status, known event dates, data completeness and policy version;
- latest accepted observation time/result identity;
- separate fetch, extraction and delivery states; last failure and next retry time.

Policies support configurable intervals, specific dates, pauses and administrator overrides. Examples supplied by the user include weekly, every three or four weeks, and very long intervals. These are configurable product choices, not established legal deadlines or inferred guarantees of unchanged status.

Keep scrape scheduling separate from renewal/deadline reminders. Whether a ten-year interval means no intermediate checks or occasional monitoring plus a long-term reminder remains a user decision. Do not automatically assign a ten-year interval without an explicit policy.

On first addition, use accepted cached data if it meets the freshness/completeness policy; otherwise queue a priority initial fetch. If cached data exists but is stale, display it with its observation date while queueing an update. A manual refresh queues priority work rather than calling Contabo's local scraper. Scheduled work becomes eligible when `next_fetch_at <= now`.

Compute the next regular fetch from an accepted successful result and the applicable policy. Failed scraping or undelivered local results must not advance the successful-fetch schedule. Preserve the original due time while retrying, so overdue work remains visible.

Use indexed, bounded due-work queries instead of scanning every trademark repeatedly. Permit one open refresh generation per application and fan the accepted update out to all authorized portfolios. Add fair capacity limits so a large import cannot indefinitely starve other users.

## 4. Durable dispatch and missed-work recovery

Contabo is the authority for which work is due. Its dispatcher runs continuously at a configurable short interval rather than only once daily. Newly added, manually requested and scheduled refreshes enter the same durable queue with different priorities.

Suggested job fields: job ID, application identity, requested observation/freshness requirement, refresh generation, due time, priority, state, worker ID, reservation token, reservation expiry, attempt counters, retry time and accepted result ID.

Claims are atomic. Use PostgreSQL queue tables initially; the current scale does not require introducing another queue service merely to establish the workflow.

At startup and periodically, reconcile all overdue and unfinished jobs, including yesterday's work. Do not reset every worker's active jobs when one worker restarts. Reassign only when the reservation/recovery rules allow it. Calendar-day changes do not invalidate open work.

## 5. Worker cycle when VPN blocks Contabo

1. Without the VPN, establish Contabo connectivity and reconcile local saved results first.
2. Upload pending eligible results and obtain acknowledgments. Fetch the latest configuration and a bounded assignment batch; save the assignments and reservation tokens locally before acknowledging them.
3. Tell Contabo the intended offline scraping window. Obtain a reservation long enough for that bounded batch, including a measured reconnect/upload allowance.
4. Connect the VPN and verify portal connectivity. Use local assignments and local portal-account/session information while scraping; do not depend on Contabo or its PostgreSQL database.
5. Save each result and its PDF/HTML/image artifacts locally before marking the item scrape-complete. Process PDFs locally; extraction must not require a production database connection.
6. Finish or checkpoint the bounded batch, release portal sessions as appropriate, disconnect the VPN, and verify connectivity to Contabo.
7. Upload metadata and artifacts, reconcile job outcomes, and retain the local bundle until Contabo acknowledges durable acceptance.
8. Fetch more work and repeat.

Use bounded time/work per VPN cycle so the worker does not remain unable to synchronize indefinitely. Continuous coordinator heartbeats or lease renewal cannot be required while the VPN blocks Contabo. A reported expected-offline period differs from an unexpected loss of contact, but is not proof that the worker is still alive.

If the reservation expires, Contabo may reassign after a configured grace period. A returning worker must reconcile before claiming or restarting work. Late results are evaluated against generation and observation time: retain useful observations under policy, reject stale completion authority, and never overwrite newer accepted data. Exactly-once scraping cannot be guaranteed across a network partition; the goal is to minimize repeated scraping and make repeated delivery harmless.

## 6. Local persistence and cache reuse

Use a local SQLite store for assignments, progress and delivery state, plus durable files for large artifacts. Write files atomically, track checksums and record a manifest containing schema version, job/generation, application number, reservation identity, observation timestamp, extraction outcome and artifact list.

When Contabo offers or asks about a job, the worker checks local storage:

| Local state | Action |
|---|---|
| Accepted matching result | Confirm acceptance with Contabo; no duplicate scrape |
| Complete matching result not acknowledged | Deliver the saved result; no duplicate scrape |
| Older result for a new scheduled refresh | Reuse only if Contabo accepts its age/completeness; otherwise scrape |
| Partial result with valid saved artifacts | Resume or retry the missing work where supported; preserve completed artifacts |
| No usable result | Scrape when connectivity and reservation permit |

Matching an application number alone is insufficient: yesterday's observation must not satisfy today's requested refresh unless the policy allows it. Server requests are received through the worker's outbound reconciliation/poll cycle, not inbound access to worker PCs.

Retain unacknowledged results across process restarts and machine reboots. Stop claiming new batches when local storage or undelivered backlog exceeds configured thresholds. Cleanup occurs only after acceptance and the configured retention period.

## 7. Failure handling

| Failure | Required behavior |
|---|---|
| No internet before scraping | Keep the assignment queued locally; back off and test again |
| VPN or portal unavailable | Preserve completed items; retry recoverable failures with backoff and account cooldown |
| VPN active and Contabo unreachable | Continue only within the saved batch/offline reservation; sync after VPN disconnect |
| Crash/reboot mid-batch | Recover durable state, reconcile reservations, deliver completed results, and resume eligible remaining work |
| Scraping finished but upload failed | Keep the bundle in the outbox and retry delivery without scraping again |
| Interrupted upload | Resume/retry safely by artifact identity and checksum |
| Upload accepted but response lost | Repeat submission with the same result ID; Contabo returns the prior acceptance |
| Reservation expired/reassigned | Reconcile generation/ownership; do not apply an obsolete completion blindly |
| PDF extraction failed | Preserve the downloaded PDF and allow extraction retry without another portal download |
| Local disk full/corrupt artifact | Pause new work, report the issue after reconnect, and expose recovery in the worker UI |

Classify retryable and permanent failures, keep attempt/error history, and provide an operator review queue after retry exhaustion. No failure path should silently treat unfinished work as complete.

## 8. Contabo result acceptance and storage

- Authenticate the worker and validate job association, schema, application identity, result age and generation.
- Deduplicate repeated delivery by result ID; verify artifacts and use server-accessible storage. A worker-local filesystem path is never a delivered artifact.
- Publish an accepted snapshot, change history and job completion consistently. Handle missing required artifacts as pending/partial according to an explicit completion policy.
- Preserve the previous successful snapshot during failures and pending refreshes.
- Record scrape observation time separately from server receipt time; delayed delivery must not make an old scrape appear newly observed.
- Acknowledge only after the required data/artifacts are durable. Include accepted/rejected/retryable outcomes so workers can manage their outbox correctly.

## 9. Contabo control panel screens

- Overview: due/overdue jobs, successes/failures, fleet availability, pending deliveries and throughput.
- Users and subscriptions: accounts, permissions, plans, limits and usage.
- Trademark inventory: portfolio counts, latest accepted data, last/next fetch and policy overrides.
- Scheduling policies: intervals, dates, status/completeness conditions, rule precedence and pause controls.
- Jobs: queue, assignment, age, progress, retry history and reconciliation outcomes.
- Fleet and portal accounts: machine lifecycle, account assignment, capacity and worker health.
- Results/documents: accepted and partial results, artifact availability and delivery history.
- Operations: alerts, audit trail, settings, backup/restore status and rollout visibility.

## 10. Local worker UI screens

- Overview: current phase, internet/portal/Contabo checks, VPN state and batch progress.
- Assignments: pending, scraping, partial, scrape-complete and server-acknowledged items.
- VPN: available configurations, active tunnel, connection/disconnection, cooldown and last error.
- Local results and uploads: saved artifacts, delivery backlog, last attempt and acknowledgment.
- Controls: pause/drain/resume, retry upload, retry extraction and reconnect.
- Diagnostics/settings: readable errors, storage usage, worker identity, server URL and configuration version. Redact credentials.

Replace the current operational HTML pages with a proper application UI. Reuse suitable existing React components and APIs, while keeping Contabo administration and local operations separate from the portfolio experience.

## Implementation order and acceptance checks

1. Define the queue/result protocol and local storage, then implement one durable end-to-end worker cycle against a controlled development environment.
2. Route additions, manual refreshes and per-TM schedules through the Contabo queue; disable competing direct-fetch paths in the new mode.
3. Build the Contabo and worker UIs over that working protocol; migrate existing user, subscription and fleet controls.
4. Verify disconnect/upload recovery, power loss, expired reservations, repeated submissions, old cached results, missing artifacts, midnight rollover and two-worker claim races.
5. Verify authorization and one application tracked by multiple users; expand from one worker to 5-6 machines and tune capacity using measured throughput.
6. Migrate live data/configuration with backups and a rollback path before removing the old operating mode.

Do not change production scheduling or deployment merely by writing this specification. The live Contabo environment and credentials have not been inspected in this review.
