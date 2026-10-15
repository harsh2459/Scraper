# Local worker frontend

The maintained source is `../ops_frontend/src/`. From `../tm_manager/frontend` run `npm run build:worker` or `npm run dev:worker`. Build output is `dist/` and is ignored by Git.

`python -m worker_agent` serves the production bundle and API on loopback port 5011. Ship the built assets with the agent: the interface needs no internet connection or external fonts. Authenticate with `WORKER_LOCAL_TOKEN`, distinct from the worker's Contabo token. No database connection is required on this machine.

See `../../deploy/FLEET-RUNBOOK.md` for fixture/live configuration and recovery behavior.
