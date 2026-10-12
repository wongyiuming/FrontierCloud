#!/usr/bin/env bash
# Isolated test-only CA/root injection. No public ports or production CA calls.
set -euo pipefail
cd "$(dirname "$0")/.."
runtime="$(mktemp -d /tmp/fc-python-certificate-nginx.XXXXXXXX)"
tag="$(basename "$runtime" | tr '.' '-')"
network="$tag"
driver="$tag-driver"
edge="$tag-nginx"
cleanup() {
  docker rm -f "$edge" "$driver" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  # Only this script's verified mktemp directory contains test private keys.
  case "$runtime" in /tmp/fc-python-certificate-nginx.*) rm -rf -- "$runtime" ;; *) exit 1 ;; esac
}
trap cleanup EXIT
mkdir -p "$runtime/control" "$runtime/published" "$runtime/private" "$runtime/runtime" "$runtime/data/.ip-security"
printf '%s\n' '# frontiercloud-ip-security-v1' > "$runtime/data/.ip-security/active-bans.tsv"
chmod 0700 "$runtime/control" "$runtime/private"
chmod 0750 "$runtime/published"
chmod 0710 "$runtime/runtime"
docker build -t frontiercloud-python-certificate-tools -f Dockerfile .
docker build -t frontiercloud-python-nginx:test-tls -f nginx/Dockerfile .
docker network create --internal "$network" >/dev/null
docker run -d --name "$driver" --cpus 1 --network "$network" \
  --network-alias certificate-agent --network-alias web \
  --mount "type=bind,source=$runtime,target=/test-certificate" \
  --workdir /app --user 0 frontiercloud-python-certificate-tools \
  python -m tests.certificate_nginx_container >/dev/null
for _ in $(seq 1 100); do
  [ -f "$runtime/control/root.crt" ] && break
  sleep .2
done
test -f "$runtime/control/root.crt"
start_edge() {
  docker run -d --name "$edge" --cpus 1 --network "$network" --network-alias nginx \
    -e TLS_ENABLED=true -e SERVER_NAME=node.4399.com -e PUBLIC_BIND_ADDRESS=0.0.0.0 \
    -e INSTANCE_NAME=tls-test -e UPLOAD_INACTIVITY_TIMEOUT=300 -e NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx \
    --mount "type=bind,source=$runtime/published,target=/run/frontiercloud-tls,readonly" \
    --mount "type=bind,source=$runtime/data,target=/app/data,readonly" \
    --mount "type=bind,source=$runtime/control/root.crt,target=/usr/local/share/ca-certificates/test-only-root.crt,readonly" \
    --entrypoint /bin/sh frontiercloud-python-nginx:test-tls \
    -c 'update-ca-certificates >/dev/null; exec /docker-entrypoint.sh nginx -g "daemon off;"' >/dev/null
}
start_edge
for _ in $(seq 1 200); do
  [ -f "$runtime/control/rotation-complete" ] && break
  if [ "$(docker inspect --format '{{.State.Running}}' "$driver")" != true ]; then
    docker logs "$driver"
    docker logs "$edge"
    exit 1
  fi
  sleep .2
done
test -f "$runtime/control/rotation-complete"
docker rm -f "$edge" >/dev/null
start_edge
touch "$runtime/control/nginx-recreated"
status="$(docker wait "$driver")"
docker logs "$driver"
test "$status" = 0
test -f "$runtime/control/result.json"
mkdir -p "${FRONTIERCLOUD_CERTIFICATE_EVIDENCE:-/tmp/frontiercloud-python-certificate-evidence}"
cp "$runtime/control/result.json" "${FRONTIERCLOUD_CERTIFICATE_EVIDENCE:-/tmp/frontiercloud-python-certificate-evidence}/nginx-real.json"
