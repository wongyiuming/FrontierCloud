#!/usr/bin/env bash
# Actual fresh default Compose; never select a deployed project or data root.
set -euo pipefail
cd "$(dirname "$0")/.."
revision="${FRONTIERCLOUD_REVISION:-}"
if ! [[ "$revision" =~ ^[0-9a-f]{40}$ ]]; then exit 1; fi
work=$(mktemp -d /tmp/fc-native-default-XXXXXXXX)
prefix="fc-native-default-$(cat /proc/sys/kernel/random/uuid)"
project=
ip_holder=
files=()
cleanup() {
  if [[ "$ip_holder" == "$prefix-"* ]]; then
    docker rm -f "$ip_holder" >/dev/null 2>&1 || true
    ip_holder=
  fi
  if [[ "$project" == "$prefix-"* && "$prefix" == fc-native-default-* && ${#files[@]} -gt 0 ]]; then
    docker compose --env-file /dev/null -p "$project" "${files[@]}" down --volumes --remove-orphans >/dev/null
  fi
}
diagnose() {
  if [[ "$project" == "$prefix-"* && ${#files[@]} -gt 0 ]]; then
    docker compose --env-file /dev/null -p "$project" "${files[@]}" logs --no-color --tail=15 nginx updater >&2 || true
  fi
}
trap diagnose ERR
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
bash scripts/build-native-images.sh "$revision"
export FRONTIERCLOUD_REVISION="$revision"
export PUBLIC_BIND_ADDRESS=127.0.0.1 HTTP_PORT=0 HTTPS_PORT=0 WEBRTC_STUN_PORT=0
export TLS_ENABLED=false SERVER_NAME=localhost DB_TYPE=sqlite
export RELEASE_BRANCH=gin_main RELEASE_SOURCE_BRANCH=gin_dev
for database in sqlite mysql; do
  project="$prefix-$database"
  export COMPOSE_PROJECT_NAME="$project" DATA_DIRECTORY="$work/data-$database"
  files=(-f docker-compose.yaml)
  if [[ "$database" == mysql ]]; then files+=(-f docker-compose.gin-mysql.yaml); fi
  files+=(-f tests/native-loopback.compose.yaml)
  compose() { docker compose --env-file /dev/null -p "$project" "${files[@]}" "$@"; }
  compose up -d --no-build --wait --wait-timeout 180
  for component in web updater nginx; do
    cid=$(compose ps -q "$component")
    test "$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$cid")" = "$project"
    image=$(docker inspect --format '{{.Image}}' "$cid")
    test "$(docker image inspect --format '{{index .Config.Labels "frontiercloud.revision"}}' "$image")" = "$revision"
    test "$(docker image inspect --format '{{index .Config.Labels "frontiercloud.runtime"}}' "$image")" = go
  done
  compose exec -T web sh -c 'test "$(id -u)" = 10001; test "$(readlink /proc/1/exe)" = /app/frontiercloud; ! command -v python; ! command -v python3; ! command -v mysql'
  compose exec -T updater sh -c 'test "$(readlink /proc/1/exe)" = /app/frontiercloud-updater; ! command -v python; ! command -v python3; ! command -v docker'
  compose exec -T web /app/frontiercloud updater-status | grep -q "\"updater_runtime_sha\":\"$revision\""
  address=$(compose port nginx 80)
  curl --fail --silent --show-error --retry 10 --retry-all-errors --retry-delay 1 --max-time 5 "http://$address/health/ready" >/dev/null
  curl --fail --silent --show-error --location --max-redirs 1 "http://$address/" | grep -q '前沿娱乐'
  test -f "$DATA_DIRECTORY/.native-runtime"
  # The receipt is deliberately 0600 and owned by the runtime UID, not the
  # unprivileged CI host user. Hash it as that UID without weakening permissions.
  receipt=$(compose exec --interactive=false -T web sha256sum /app/data/.native-runtime | cut -d' ' -f1)
  if [[ "$database" == sqlite ]]; then
    ! compose config --services | grep -qx mysql
    test "$(stat -c %a "$DATA_DIRECTORY/frontiercloud.db")" = 600
  else
    test ! -e "$DATA_DIRECTORY/frontiercloud.db"
  fi
  compose stop web
  compose restart redis
  if [[ "$database" == mysql ]]; then compose restart mysql; fi
  compose up -d --no-build --wait --wait-timeout 180
  test "$(compose exec --interactive=false -T web sha256sum /app/data/.native-runtime | cut -d' ' -f1)" = "$receipt"
  curl --fail --silent --show-error --retry 10 --retry-all-errors --retry-delay 1 --max-time 5 "http://$address/health/ready" >/dev/null
  # Force a different Web address while keeping Nginx alive. Merely restarting
  # Web can accidentally reuse the old IP and conceal stale upstream DNS.
  nginx_before=$(compose ps -q nginx)
  web_before=$(compose ps -q web)
  old_ip=$(docker inspect --format "{{with index .NetworkSettings.Networks \"${project}_default\"}}{{.IPAddress}}{{end}}" "$web_before")
  test -n "$old_ip"
  compose stop web
  compose rm -f web
  ip_holder="$project-ip-holder"
  # Let the Engine allocate the holder address. Engines with automatic IPAM
  # reject --ip on their default subnet; consuming the next free address works
  # with both first-free and sequential allocators. Assert actual change below.
  docker run -d --name "$ip_holder" --network "${project}_default" \
    --entrypoint /bin/sleep "frontiercloud-go-nginx:$revision" 120 >/dev/null
  compose up -d --no-build --no-deps --wait --wait-timeout 180 web
  new_ip=$(docker inspect --format "{{with index .NetworkSettings.Networks \"${project}_default\"}}{{.IPAddress}}{{end}}" "$(compose ps -q web)")
  test -n "$new_ip"
  test "$new_ip" != "$old_ip"
  test "$(compose ps -q nginx)" = "$nginx_before"
  curl --fail --silent --show-error --retry 10 --retry-all-errors --retry-delay 1 --max-time 5 "http://$address/health/ready" >/dev/null
  curl --fail --silent --show-error --location --max-redirs 1 "http://$address/" | grep -q '前沿娱乐'
  test "$(compose exec --interactive=false -T web sha256sum /app/data/.native-runtime | cut -d' ' -f1)" = "$receipt"
  cleanup
  project=
  printf 'PASS: actual native default %s bootstrap, no Python, persistent store and cache restart\n' "$database"
done
# Retain only this private host child for bounded diagnostics; no global prune.
