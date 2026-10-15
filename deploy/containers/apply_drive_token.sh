#!/usr/bin/env bash
# Put a freshly authorized Google Drive login into the live Docker settings, then restart the web app.
#   bash apply_drive_token.sh /root/.tmtracker-drive/authorization.json
# Never prints the secrets. Keeps timestamped backups of the settings files it changes.
set -euo pipefail
FILE="${1:?Usage: apply_drive_token.sh /path/to/authorization.json}"
[ "$(id -u)" = 0 ] || { echo "Run as root." >&2; exit 1; }
STAMP=$(date +%Y%m%dT%H%M%S)
python3 - "$FILE" "$STAMP" <<'PY'
import json, shutil, sys
from pathlib import Path
bundle = json.load(open(sys.argv[1]))
keys = ('GOOGLE_DRIVE_CLIENT_ID', 'GOOGLE_DRIVE_CLIENT_SECRET', 'GOOGLE_DRIVE_REFRESH_TOKEN')
assert all(bundle.get(k) for k in keys), 'authorization file is incomplete'
for name in ('live-user.env', 'live-control.env'):
    path = Path('/etc/tmtracker') / name
    shutil.copy2(path, str(path) + '.before-drive-' + sys.argv[2])
    text = path.read_text().splitlines()
    if name == 'live-control.env' and not any(l.split('=', 1)[0] in keys for l in text):
        continue  # the control service did not use Drive before; leave it alone
    lines = [l for l in text if l.split('=', 1)[0] not in keys]
    lines += [f'{k}={bundle[k]}' for k in keys]
    path.write_text('\n'.join(lines) + '\n')
    path.chmod(0o600)
    print('updated', path)
PY
RELEASE=$(python3 -c 'import json; print(json.load(open("/etc/tmtracker/live-state.json"))["release"])')
docker compose --env-file /etc/tmtracker/live-compose.env -f "$RELEASE/deploy/containers/compose.production.yaml" up -d --force-recreate --no-build control user
sleep 15
docker logs --tail 5 tmtracker-live-user-1 2>&1 | grep -ci "invalid_grant" | sed 's/^/invalid_grant lines in last 5 log lines: /'
echo "Done. Open a reminder in the app to confirm it loads."
