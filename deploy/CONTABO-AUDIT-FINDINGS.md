# Contabo audit findings — 5 October 2026

Source: the privately transferred `.runtime/contabo-audit.json`, observed at
06:33:49 UTC. Inspection was read-only. No deployment, migration, upload or
cleanup was performed by the inspection.

## Existing deployment

| Component | Observed state |
| --- | --- |
| Application | `/opt/tmtracker`, approximately 49 GB |
| Documents | `/opt/tmtracker/data/ipindia`, approximately 47 GB |
| Journal | Approximately 1.5 GB |
| Disk | 96 GB total, 55 GB used, 41 GB available |
| Database | `datascraper`, PostgreSQL 16.15, 1,920,998,423 bytes |
| Schemas | `public` only; no new `fleet_control` schema |
| User backend | Existing native systemd service running |
| PDF scraper | Existing native systemd service running |
| Main scraper | `activating / auto-restart`; cause not captured |
| New coordinator | No installed systemd unit; inactive |
| Docker / Compose | Not installed |
| Public routing | Existing Nginx and TLS for `mytmtracker.com` and `scraper.mytmtracker.com` |

The legacy `fleet_machines` and `fleet_assignments` tables already exist. They
are distinct from the new coordinator tables and must not be overwritten or
treated as registered workers in the new fleet without a deliberate migration.

## Data to preserve

Existing tables include users, portfolios, private notes, compliance status,
status history, journal entries and matches, wordmark searches/results, leads,
old fleet assignments, documents and Drive references. The migration must
preserve these, not just the scraped application records.

`wordmark_search_results` occupies 1,751,752,704 bytes, approximately 91% of the
database size. This is database storage; the 47 GB document directory is a
separate disk consumer. Estimated row counts in this audit are not exact counts.

| Local-path document records | Records | Without Drive ID | With ID, cleanup pending |
| --- | ---: | ---: | ---: |
| TM-A | 21,441 | 4,267 | 17,174 |
| Reminders | 843 | 313 | 530 |
| Correspondence | 7,382 | 4,145 | 3,237 |
| Total | 29,666 | 8,725 | 20,941 |

An ID records a reference; it does not prove the currently configured account
can read the file. The previously sampled Drive file returned 404. Retain local
originals until access and checksums are verified. Counts above do not establish
that every file in the 47 GB directory is represented in the database.

The scraper's Drive root-folder setting is blank while the user backend's is
present. The report contains no folder IDs or account identities. This difference
needs reconciliation after applying the correct authorization; it does not by
itself establish why the prior file lookup failed.

## Initial schema comparison

The audited columns used by the new legacy projection are present in
`my_trademarks`, `ipindia_results`, `ipindia_rows`, `ipindia_page_fetches`,
`ipindia_trademark_images`, `tm_status_history`, `ipindia_reminders`, and
`ipindia_correspondence`. Existing TM-A extraction and Drive-reference columns
are present. `fleet_canonical` and `fleet_sha256` are absent from `tma_documents`;
numbered migration 006 adds them without removing existing rows.

This is a column-level comparison, not migration acceptance. The report omits
column defaults, constraint definitions, index definitions and triggers. A
schema-only export is needed to reproduce the actual legacy schema locally and
test the additive migrations. Later, backfill acceptance must also use a private
staging restore of real data: an empty schema cannot validate real-row behavior.

The application's actual database-name setting is `PG_DBNAME`; the first audit
checked `PG_DATABASE` instead. Its absence is not a configuration failure. The
inspection helper now reports both keys, without values.

## Deployment decision

Use Docker Compose for the new Linux application services, keep the existing
host PostgreSQL and Nginx/TLS for the first rollout, and keep WireGuard workers
native on Windows. Preserve `/opt/tmtracker` document paths through explicit
mounts. Do not copy the 47 GB document tree into an image or replace the database
with the local demo database.

Complete verified Drive delivery, central worker diagnostics, normal administrator
sign-in, and container packaging before switching public routing. Test the
existing schema first and resolve the legacy scraper restart loop before deciding
how to cut over job ownership. There must be one scheduler for each work stream.

## Schema rehearsal completed

The schema-only export below was received and restored into a new isolated local
database. Its real defaults, foreign keys and indexes restored successfully,
including `pg_trgm` as an explicit prerequisite. All twelve fleet migrations
passed; a second migration run was unchanged. Synthetic original user ownership,
notes, document paths, Drive IDs and compliance status were preserved.

Two simulated worker deliveries then published through the new coordinator into
the restored legacy schema. Lost-acknowledgment retries were idempotent, repeated
PDF extraction retained one content row and one canonical row, and historical
Drive IDs remained unchanged. No Google Drive request or VPN connection was made.

The retained local database and checks are recorded privately in
`.runtime/contabo-migration-rehearsal.json`. The test server is PostgreSQL 18.6;
Contabo is 16.15. Real-row backfill and PostgreSQL 16 staging acceptance remain
required. Staging-only Docker files are under `deploy/containers`; Compose
configuration validation and three staging guard tests passed. No Docker Engine
build or container execution has been verified.

## Completed read-only export commands

On Contabo, create a private schema-only export with no customer rows:

```bash
umask 077
sudo -u postgres pg_dump --dbname=datascraper --schema-only --schema=public --no-owner --no-privileges > /root/contabo-schema.sql
```

On the PC, copy it into the private runtime directory:

```powershell
cd H:\Datascraper-updates
scp root@62.146.239.158:/root/contabo-schema.sql .runtime/contabo-schema.sql
```

Keep the export private: schema definitions can contain application-specific
defaults and function bodies. Do not paste it into chat. This command reads the
database; it does not change live data, services or routing.
