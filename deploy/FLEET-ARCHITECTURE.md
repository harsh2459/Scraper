# Contabo coordinator and VPN scraping fleet

This is an architecture assessment of the local source snapshot, not a verification of the deployed Contabo configuration. No live server or database was accessed.

The user's subsequent requirements are recorded in `FLEET-REQUIREMENTS.md`. The implemented worker defaults to bounded offline reservations and delivery after VPN disconnect. Inspection of the copied configs subsequently found destination-based split routing; VPN use does not necessarily disconnect Contabo. Optional online transport is now implemented behind `WORKER_NETWORK_MODE=split`, with verified routes/HTTPS and durable recovery. See [VPN-ROUTING-PLAN.md](VPN-ROUTING-PLAN.md) for configuration facts, isolated verification and remaining active-tunnel pilot checks. Administration moves into a dedicated Contabo control panel; TM Manager remains the user portfolio interface.

The current implementation roadmap is `IMPLEMENTATION-PLAN.md`. It specifies two Contabo backends sharing PostgreSQL: the user backend owns users/subscriptions/portfolios, and the scraper management backend owns scheduling/fleet/results. It supersedes earlier proposals where they differ.

The first runnable release is now implemented in this copied workspace. See [FLEET-RUNBOOK.md](FLEET-RUNBOOK.md) for the verified fixture workflow, deployment preparation and remaining rollout work. The assessment below describes the original snapshot, not the completed implementation status.

## Required workflow

1. A user adds a trademark or requests a refresh in TM Manager on Contabo. Contabo also schedules existing trademarks when their refresh interval expires.
2. Contabo saves a durable scraping job. Fleet machines claim available work over authenticated HTTPS, according to priority and available capacity.
3. The selected worker fetches IP India data through its VPN and passes downloaded documents to its local PDF service.
4. The worker stores the result and documents durably before attempting delivery. It sends them to Contabo and retains its local copy until Contabo acknowledges receipt.
5. Contabo validates and stores the result, updates the trademark and its change history, and exposes the updated data to the owning users.

Contabo owns application data, user permissions, scheduling, job ownership, and result acceptance. Workers own scraping sessions, VPN state, PDF extraction, and undelivered local results. Adding another worker must not require changing application code.

## Existing implementation

- `tm_manager/app.py` saves user portfolio additions and provides refresh routes.
- `tm_manager/scheduler.py` requeues old records and runs batch workers.
- `tm_manager/fleet_scheduler.py` runs a daily coordinator planning pass.
- `tm_manager/db.py` distributes work across active machines with recent heartbeats, weighted by assigned accounts or an administrator's capacity hint. Its worker claim uses database locks.
- Fleet workers register, heartbeat, claim work, and write results directly to the shared PostgreSQL database. The worker scheduler does not currently use the coordinator assignment API as its main work transport.
- `pdf_scraper/` saves and extracts downloaded documents. Drive storage is optional; otherwise files remain on the worker's disk.
- `vpn_rotator.py` manages machine-wide WireGuard tunnels on Windows.
- `offline_queue.py` buffers Auto Discover and series-scan results, explicitly excluding fleet batches. `pending_sync.py` buffers certain document metadata writes.

## Gaps affecting the required workflow

1. **Add and live refresh bypass the fleet.** `/api/add` starts `fast_fetch_new_trademarks`; live refresh starts `_run_fast_fetch_one`. Both call the scraper configured on that TM Manager instance, defaulting to localhost. These paths must enqueue priority fleet work on Contabo.
2. **Planning is daily.** New work appearing after the daily pass is not assigned automatically until another pass or a manual replan. Planning needs an ongoing dispatch mechanism.
3. **Assignments can become stranded across midnight.** Planning excludes all open assignments, including previous days, but fleet claims only select `work_date = current_date`. Old open assignments therefore block replanning while remaining unclaimable.
4. **Restart recovery is global.** Every TM Manager startup resets all `fetching` trademarks in the shared database. Restarting one worker can reset work another worker is actively processing. Recovery must be based on expired job ownership leases.
5. **VPN scraping still depends on remote PostgreSQL.** Fleet claims and result persistence require database access; the full offline result queue excludes this workflow. Transport must tolerate VPN changes and interrupted connectivity.
6. **Database rows do not transfer documents.** Worker-local paths are unusable on Contabo. Upload PDF, HTML, and image bytes to server-accessible storage, and acknowledge them before completing the job. Existing Drive support can be reused where configured.
7. **Worker authentication is too broad.** Existing fleet API routes rely on the global login/ops gate; ops credentials grant administrator access, and several fleet blueprint routes do not require a dedicated worker identity. Use individual revocable worker credentials scoped to that machine's jobs.

## Proposed implementation

Use PostgreSQL on Contabo as the durable queue initially; a separate queue service is not required to establish this workflow.

### Job ownership

Each job records its application number, refresh generation, priority, due time, state, attempt count, next retry time, worker identity, lease token, lease expiry, and result identity. Keep user portfolio membership separate from the scrape job so the same public application can be fetched once and update multiple subscribed portfolios.

States: `queued -> leased -> completed`; retryable failures return to `queued` after backoff, and exhausted or permanent failures become `failed`. A worker's heartbeat and its job lease renewal are separate operations. A machine restart must not reset another machine's work.

Claims must be atomic. In the implemented worker, bounded reservations cover scraping without lease renewal; the worker reconciles after reconnecting. Optional split transport reports heartbeats and delivers within the same finite reservation budget; it does not extend ownership locally. Expired reservations become eligible for another machine. Reject stale completion tokens; identify result submissions uniquely so delivery retries cannot create duplicate history or overwrite a newer accepted refresh.

### Worker API

| Operation | Purpose |
|---|---|
| Register/heartbeat | Identify a worker and report capacity, health, and VPN connectivity |
| Claim | Atomically lease a small priority-ordered batch for the authenticated machine |
| Renew | Extend ownership while a job is progressing |
| Upload artifact | Store documents and images with checksums and stable identifiers |
| Complete | Validate and persist a result for the current lease; return a durable acknowledgment |
| Fail | Record an error and apply server-controlled retry policy |

Workers initiate outbound HTTPS connections; Contabo does not need inbound access to office PCs. Workers should not need production database credentials or administrator credentials after migration.

### Durable delivery and VPN handling

Persist claimed jobs and finished result bundles in a local worker outbox. Retain bundles until Contabo acknowledges durable acceptance. Resume delivery after restart and network recovery. Verify connectivity to both the portal and coordinator; a VPN connection alone is not a successful health check. Use split routing where the deployment supports it, but retain the outbox regardless.

The PDF service must return extracted metadata and artifact references without requiring production database writes in the new worker transport path. Reuse the existing extraction functions.

### User updates

Retain the last accepted trademark data while a refresh is queued or running. Show queued, updating, last successful update, and failure state separately. Contabo computes status/document changes and updates all relevant portfolios. Refresh the frontend through bounded polling initially; event streaming can be added later if needed.

## Migration and verification

1. Add the queue, scoped worker authentication, lease operations, artifact upload, and idempotent result acceptance behind an explicit rollout setting.
2. Move additions, manual refresh, and periodic refresh onto the same queue. In this mode, disable Contabo's competing local fetch paths and global startup reset.
3. Adapt one worker and its PDF service to the local outbox and HTTPS transport. Verify against a controlled development database before live rollout.
4. Verify two workers racing to claim one job, worker loss and lease expiry, stale result rejection, repeated result delivery, interrupted uploads, VPN disconnect, worker restart, midnight rollover, and one application shared by multiple users.
5. Roll out to the existing worker, then add 5–6 machines with distinct credentials, machine names, and assigned portal accounts. Monitor queued age, delivery backlog, completion rate, retries, and worker health before expanding further.

Final machine capacity and refresh frequency should be set from measured portal throughput and the number of trademarks due for refresh. The current per-account daily capacity is a planning estimate, not verified throughput.
