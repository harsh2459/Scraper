#!/usr/bin/env bash
# Update the LIVE production release in place from a newer code-only bundle (see deploy/build_contabo_bundle.py).
#
#   bash update_live.sh /path/to/extracted/bundle           # do the update
#   bash update_live.sh /path/to/extracted/bundle --check   # only check the server and say what would happen
#
# Order: checks -> backups (database, current files, current image) -> copy files -> database changes
# -> grants -> rebuild image -> recreate control + user containers -> health checks.
# Database changes in this project are additive, so if the health checks fail the script puts the old
# files and image back and restarts the old containers; the added tables/columns are harmless to old code.
# Run as root on the Contabo server. Secrets are never printed.
set -euo pipefail

NEW="${1:?Usage: update_live.sh /path/to/extracted/bundle [--check]}"
MODE="${2:-}"
STATE=/etc/tmtracker/live-state.json
STAMP=$(date +%Y%m%dT%H%M%S)
BK=/root/tmtracker-backups/update-$STAMP

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "Run as root."
[ -d "$NEW/deploy/containers" ] && [ -f "$NEW/release-manifest.json" ] || die "$NEW is not an extracted release bundle."
[ -f "$STATE" ] || die "$STATE not found: is this the production server?"
RELEASE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release"])' "$STATE")
[ -d "$RELEASE/deploy/containers" ] || die "Live release folder $RELEASE is missing."
COMPOSE=(docker compose --env-file /etc/tmtracker/live-compose.env -f "$RELEASE/deploy/containers/compose.production.yaml")
TAG=$(grep '^RELEASE_TAG=' /etc/tmtracker/live-compose.env | cut -d= -f2-)
IMAGE="tmtracker-live:$TAG"
ADMIN_TOKEN=$(grep '^CONTROL_ADMIN_TOKEN=' /etc/tmtracker/live-control.env 2>/dev/null | cut -d= -f2- || true)

health() { curl -fsS --max-time 8 http://127.0.0.1:5010/api/control/v1/health 2>/dev/null | grep -q '"status":"ok"'; }

say "Checking the server"
health || die "The live control service is not healthy right now. Fix that first; nothing was changed."
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "Live image $IMAGE not found."
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
[ "$FREE_GB" -ge 4 ] || die "Only ${FREE_GB} GB free on /; need at least 4 GB for backups and the image build."
echo "Live release : $RELEASE"
echo "Live image   : $IMAGE"
echo "Free disk    : ${FREE_GB} GB"
echo "Pending files: $(find "$NEW" -type f | wc -l) (code only)"
if [ "$MODE" = "--check" ]; then say "Check passed. Run again without --check to update."; exit 0; fi

say "Backing up (database, current release files, current image) to $BK"
mkdir -p "$BK" && chmod 700 "$BK"
sudo -u postgres pg_dump -Fc -d datascraper > "$BK/datascraper.dump"
pg_restore --list "$BK/datascraper.dump" > /dev/null || die "The database backup could not be read back."
tar -czf "$BK/release-files.tar.gz" -C "$RELEASE" shared-scraper deploy
# nginx serves the web pages straight from this folder, so it must be refreshed from the new image too.
WEB="/var/www/tmtracker-live/$TAG"
[ -d "$WEB" ] && tar -czf "$BK/web-files.tar.gz" -C "$WEB" .
docker tag "$IMAGE" "$IMAGE-before-$STAMP"
echo "Backup ready. To undo later: restore release-files.tar.gz into $RELEASE and retag $IMAGE-before-$STAMP as $IMAGE."

rollback() {
  printf '\n!! The update did not pass its health checks. Putting the previous version back.\n' >&2
  tar -xzf "$BK/release-files.tar.gz" -C "$RELEASE" || true
  docker tag "$IMAGE-before-$STAMP" "$IMAGE" || true
  [ -f "$BK/web-files.tar.gz" ] && tar -xzf "$BK/web-files.tar.gz" -C "$WEB" || true
  "${COMPOSE[@]}" up -d --force-recreate --no-build control user || true
  sleep 8
  if health; then echo "Previous version is running again. The database additions are left in place (they do not affect it)." >&2
  else echo "Could not confirm the previous version is healthy. Backup folder: $BK" >&2; fi
  exit 1
}

show_logs() {
  for c in tmtracker-live-control-1 tmtracker-live-user-1; do
    echo "---- last log lines of $c" >&2
    docker logs --tail 25 "$c" 2>&1 | sed -E 's/((token|password|secret|key)[A-Za-z_]*[=:] *)[^ ,"]+/\1***/Ig' >&2 || true
  done
}
fail() { echo "$1" >&2; show_logs; rollback; }

say "Copying the new code into the live release folder"
cp -a "$NEW"/. "$RELEASE"/
chmod -R a+rX "$RELEASE"

say "Applying database changes (additive; already-applied ones are skipped)"
sudo -u postgres /opt/tmtracker/.venv/bin/python "$RELEASE/deploy/containers/migrate_live.py"

say "Granting the control service access to the new tables"
sudo -u postgres psql -d datascraper -v ON_ERROR_STOP=1 -q <<'SQL'
GRANT SELECT ON fleet_control.tm_owner_priority TO tm_live_control;
GRANT SELECT, INSERT, UPDATE, DELETE ON fleet_control.enrollment_keys TO tm_live_control;
SQL

say "Building the new image (a few minutes)"
"${COMPOSE[@]}" build control || { echo "Build failed; the running site was not touched." >&2; tar -xzf "$BK/release-files.tar.gz" -C "$RELEASE"; exit 1; }

say "Restarting the control and user services (a few seconds of downtime)"
"${COMPOSE[@]}" up -d --force-recreate control user

say "Publishing the new web pages for nginx"
mkdir -p "$WEB" && docker cp tmtracker-live-user-1:/opt/tmtracker/tm_manager/frontend/dist/. "$WEB"/ && chmod -R a+rX "$WEB"

say "Health checks (up to 2 minutes; the services take a while to start)"
CONTROL_OK=0; USER_CODE=000
for i in $(seq 1 60); do
  health && CONTROL_OK=1
  USER_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:5005/api/me || true)
  if [ "$CONTROL_OK" = 1 ] && [ "$USER_CODE" = 401 ]; then break; fi
  sleep 2
done
[ "$CONTROL_OK" = 1 ] || fail "The control service did not become healthy."
[ "$USER_CODE" = 401 ] || fail "The user service did not come up (last HTTP answer $USER_CODE; 401 would mean up and asking for sign-in)."
echo "  control service healthy, user service up"
# Old code answers 401 here (it wants a login); the new code answers 403 (key refused). 403 proves the new code is live.
ENROLL=000
for i in 1 2 3 4 5; do
  ENROLL=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -X POST http://127.0.0.1:5010/api/control/v1/enroll -H 'Content-Type: application/json' -d '{"key":"update-check"}' || true)
  [ "$ENROLL" = "403" ] && break; sleep 2
done
[ "$ENROLL" = "403" ] || fail "The new control code does not seem to be running (enroll check returned $ENROLL)."
if [ -n "$ADMIN_TOKEN" ]; then
  for path in status-schedule queue-overview accounts-overview enrollment-keys; do
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H "Authorization: Bearer $ADMIN_TOKEN" "http://127.0.0.1:5010/api/control/v1/admin/$path" || true)
    echo "  admin/$path -> $CODE"
    [ "$CODE" = "200" ] || fail "admin/$path answered $CODE instead of 200."
  done
fi
PUBLIC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 --resolve mytmtracker.com:443:127.0.0.1 https://mytmtracker.com/api/control/v1/health || true)
echo "  public https health -> $PUBLIC"
[ "$PUBLIC" = "200" ] || fail "The public HTTPS health check answered $PUBLIC."

say "Update complete"
echo "Applied database versions (latest 4):"
sudo -u postgres psql -d datascraper -At -c "select version from public.fleet_schema_migrations order by version desc limit 4"
docker ps --format '  {{.Names}}  {{.Status}}' | grep -i tmtracker-live || true
echo "Backups: $BK"
echo "Next: open https://mytmtracker.com/admin/ , go to Status schedule and press 'Fill missing with recommended days'."
