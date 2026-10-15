#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $(id -u) == 0 ]] || { echo 'Run on Contabo as root.'; exit 1; }
cd "$(dirname "$0")/../.."
python3 deploy/containers/verify_release.py
python3 deploy/containers/prepare_server_staging.py
compose=(docker compose --env-file /etc/tmtracker/staging-compose.env -f deploy/containers/compose.staging.yaml)
"${compose[@]}" config --quiet
"${compose[@]}" build
"${compose[@]}" --profile migration run --rm migrate
python3 deploy/containers/finish_server_staging.py
"${compose[@]}" up -d control user
python3 deploy/containers/check_server_staging.py
frontend=/var/www/tmtracker-staging
install -m 0755 -d "$frontend"
container=$(docker create tmtracker-staging:contabo-staging)
trap 'docker rm "$container" >/dev/null 2>&1 || true' EXIT
docker cp "$container:/opt/tmtracker/tm_manager/frontend/dist/." "$frontend/"
docker rm "$container" >/dev/null
trap - EXIT
chmod -R a+rX "$frontend"
nginx_stage=/etc/nginx/conf.d/tmtracker-staging.conf
if [[ -e "$nginx_stage" ]] && ! cmp -s deploy/containers/staging-nginx.conf "$nginx_stage"; then
  echo 'Existing staging Nginx file differs; stop for review.'
  exit 1
fi
install -m 0644 deploy/containers/staging-nginx.conf "$nginx_stage"
nginx -t
systemctl reload nginx
curl --fail --silent http://127.0.0.1:5585/ >/dev/null
curl --fail --silent http://127.0.0.1:5585/admin/ >/dev/null
echo 'Server staging checks passed. This has NOT switched the public website.'
echo 'Private backup location is saved in /etc/tmtracker/staging-state.json.'
