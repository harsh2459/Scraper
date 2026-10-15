# Administration frontend

The maintained source is `../ops_frontend/src/`, shared with the local worker frontend. User, plan and subscription pages reuse the existing TM Manager components. `dist/` is generated and ignored by Git; build it before deployment.

From `../tm_manager/frontend` run `npm run build:admin` or `npm run dev:admin`. The control API serves the production bundle at `/admin/`. In development the existing portfolio Vite server proxies `/admin/` and `/api/control/` to port 5010.

Fleet operations use the control administrator bearer credential; user administration uses the existing admin account and user API. See `../../deploy/FLEET-RUNBOOK.md` for the complete operating contract and staged rollout.
