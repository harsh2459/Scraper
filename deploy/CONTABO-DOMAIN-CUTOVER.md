# Confirmed production address

The user's production website is `https://mytmtracker.com`. The SSH tunnel and
`127.0.0.1:5585` are temporary staging access, not the intended public deployment.

| Public path | Production destination |
| --- | --- |
| `/` | New portfolio frontend using the live database |
| `/admin` | Redirect to `/admin/` |
| `/admin/` | Fleet administration frontend |
| `/api/control/v1/admin/` | User backend's existing-administrator session relay |
| Other `/api/control/` paths | Coordinator with its worker/service authentication |
| Existing `/api/` and authorized document routes | User backend |

The current screenshot's "Coming soon" page is the old frontend. Starting the
staging containers did not change the public Nginx routes.

## Cutover requirements

Do not point these public routes at ports 5505/5510: those services use a private
snapshot database, not live `datascraper`. Staging HTTP cookies also deliberately
use different security settings from a public HTTPS deployment.

Prepare a production release and service configuration against live `datascraper`,
retain existing user/document IDs and configure secure cookies. Finish verified
Drive-backed fleet delivery/downloads and central worker diagnostics. Verify the
administrator login and historical document access in staging first.

For production, preserve the existing Nginx certificate-bearing server blocks.
The updated routing snippet in `deploy/nginx-fleet.locations.conf` describes the
intended routes; it is not a standalone replacement for the live site file.
Take a fresh private backup, stop overlapping schedulers, apply tested additive
migrations, verify new service readiness, then test and reload Nginx. Keep
previous routes/services available for rollback. Do not weaken the staging
entrypoint's database checks to turn the test image into a production release.
