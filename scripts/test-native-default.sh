#!/usr/bin/env bash
# Actual fresh default Compose; never select a deployed project or data root.
set -euo pipefail
cd "$(dirname "$0")/.."
revision="${FRONTIERCLOUD_REVISION:-}"
if ! [[ "$revision" =~ ^[0-9a-f]{40}$ ]]; then exit 1; fi
work=$(mktemp -d /tmp/fc-native-default-XXXXXXXX)
prefix="fc-native-default-$(cat /proc/sys/kernel/random/uuid)"
project=
files=()
cleanup() {
  if [[ "$project" == "$prefix-"* && "$prefix" == fc-native-default-* && ${#files[@]} -gt 0 ]]; then
    docker compose --env-file /dev/null -p "$project" "${files[@]}" down --volumes --remove-orphans >/dev/null
  fi
}
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
  address=$(compose port nginx 80)
  curl --fail --silent --show-error "http://$address/health/ready" >/dev/null
  curl --fail --silent --show-error "http://$address/" | grep -q '前沿娱乐'
  test -f "$DATA_DIRECTORY/.native-runtime"
  receipt=$(sha256sum "$DATA_DIRECTORY/.native-runtime" | cut -d' ' -f1)
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
  test "$(sha256sum "$DATA_DIRECTORY/.native-runtime" | cut -d' ' -f1)" = "$receipt"
  curl --fail --silent --show-error "http://$address/health/ready" >/dev/null
  cleanup
  project=
  printf 'PASS: actual native default %s bootstrap, no Python, persistent store and cache restart\n' "$database"
done
# Retain only this private host child for bounded diagnostics; no global prune.
