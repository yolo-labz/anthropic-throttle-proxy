#!/usr/bin/env bash
# Existing SSH trust and separately provisioned private auth/TLS files are required.
# Context must already contain the hash-checked Quarto installer as quarto.deb.
set -euo pipefail
HOST="${SCIENCE_DOKKU_HOST:-ProxMox.Dokku}"
ssh "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
mount_once() {
  local app="$1" mount="$2" existing
  existing="$(dokku storage:list "$app")"
  case "$existing" in
    *"$mount"*) ;;
    *) dokku storage:mount "$app" "$mount" ;;
  esac
}
if ! sudo docker network inspect token-science >/dev/null 2>&1; then
  dokku network:create token-science
fi
sudo env DOCKER_BUILDKIT=0 docker build --cpu-quota=200000 --memory=3g \
  -t token-science:20261114 /tmp/token-science-build
sudo env DOCKER_BUILDKIT=0 docker build --cpu-quota=100000 --memory=1g \
  -f /tmp/token-science-build/Dockerfile.tracking \
  -t token-science:tracking-20261114 /tmp/token-science-build
research_id="$(sudo docker image inspect --format '{{.Id}}' token-science:20261114)"
tracking_id="$(sudo docker image inspect --format '{{.Id}}' token-science:tracking-20261114)"
research_image="token-science:release-${research_id#sha256:}"
tracking_image="token-science:release-${tracking_id#sha256:}"
sudo docker tag "$research_id" "$research_image"
sudo docker tag "$tracking_id" "$tracking_image"
for app in token-research token-experiments; do
  if ! dokku apps:exists "$app"; then dokku apps:create "$app"; fi
  dokku network:set "$app" initial-network token-science
  sudo install -d -m 0700 -o 10001 -g 10001 "/var/lib/dokku/data/storage/$app"
  dokku nginx:set "$app" bind-address-ipv4 127.0.0.1
  dokku nginx:set "$app" bind-address-ipv6 ::1
  dokku nginx:set "$app" client-max-body-size 64m
  mount_once "$app" "/var/lib/dokku/data/storage/$app:/workspace"
  mount_once "$app" /var/lib/dokku/data/storage/token-science-auth:/run/science-auth:ro
  dokku resource:limit --process-type web --cpu 1 --memory 2g "$app"
  dokku config:set --no-restart "$app" PORT=5000 MLFLOW_DISABLE_AGENT_HINT=1
done
mount_once token-research /var/lib/dokku/data/storage/dagster:/lake:ro
mount_once token-research /var/lib/dokku/data/storage/token-science-tls:/run/science-tls:ro
dokku config:set --no-restart token-research \
  SCIENCE_SCHEDULER=tls://100.84.167.121:18786 \
  SCIENCE_TLS=/run/science-tls \
  MLFLOW_TRACKING_URI=http://token-experiments.web.1:5000
dokku ports:set token-research http:18400:5000
dokku ports:set token-experiments http:18401:5000
dokku resource:limit --process-type publisher --cpu 1 --memory 1g token-research
dokku git:from-image token-research "$research_image"
dokku ps:scale token-research web=1 publisher=1
dokku git:from-image token-experiments "$tracking_image"
REMOTE
