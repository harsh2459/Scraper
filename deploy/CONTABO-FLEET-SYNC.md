# Central fleet visibility on the live Datascraper server

The user selected the live Contabo Datascraper server as the central destination on 5 October 2026. Its HTTPS address and deployment state have not yet been supplied. No live server connection, deployment or data migration has been performed for this request.

## Current sync coverage

| Information | Current implementation | Remaining integration |
|---|---|---|
| Worker identity, capacity and administrative state | Coordinator workers table and admin API | Register the real machine with the live coordinator |
| Latest worker contact and execution phase | Worker heartbeat, stored centrally | Point the real worker to the live HTTPS coordinator |
| Local assignment/result counts | Heartbeat metadata | Display and verify live counts |
| Requested/effective networking mode and route diagnostics | Heartbeat metadata | Verify active VPN and coordinator connectivity |
| Queued jobs, reservations, attempts and schedules | Coordinator PostgreSQL | Deploy the new coordinator and migrations to the intended database |
| Accepted results and document artifacts | Durable worker outbox, HTTPS delivery and coordinator acceptance | Verify a real authenticated acceptance cycle |
| VPN inventory and selected configuration | Config files are on the worker; current coordinator metadata does not include the full inventory | Add key-free inventory reporting and a central view |
| Worker disk space, pause state and latest local error | Local status API | Include these operational fields in central telemetry |
| Worker activity history | Local SQLite events; coordinator has its own audit log | Add bounded, sanitized local activity synchronization if required |
| Operator sign-in | Separate control credential and user sign-in currently exist | Integrate central fleet access with the existing administrator login |

## Intended operator experience

The operator signs into the live Datascraper dashboard and sees each machine, VPN availability, current work, delivery backlog and last reported error. Workers use their provisioned service credentials automatically. A local worker dashboard remains available for diagnostics; opening it is not required to keep the fleet running.

Private WireGuard keys and raw config files remain on the owning worker. Inventory reports contain configuration names, country, selected configuration and public readiness information. Never put service credentials, portal passwords, private keys or raw response bodies in telemetry. Local SQLite and documents remain durable until server acknowledgment; centralized visibility does not replace the outbox.

## Live integration order

1. Obtain the live HTTPS address and establish whether that deployment contains the new `/api/control/v1` coordinator or only the older `/api/fleet` interface. These interfaces are separate implementations; do not assume they are interchangeable or duplicate job ownership across them.
2. Inspect the intended server deployment and prepare the central telemetry/sign-in changes for that implementation. Preserve existing user data; local synthetic demo records are not a live migration source.
3. Deploy the coordinator before updated workers. Verify public health and authenticated administrative access, then provision a unique worker service credential.
4. Add key-free worker inventory and operational telemetry, configure the real worker's HTTPS destination and verify heartbeat storage centrally.
5. Run the prepared one-machine VPN pilot with an administrator session. Confirm portal routing, ordinary coordinator routing and owned-tunnel cleanup.
6. Verify one real assignment, durable artifact upload, result acceptance, reconnect recovery and dashboard visibility before adding more workers.

The address and deployment identification are required before changing a worker to a live destination. Current local services remain in fixture mode until the live integration is prepared.
