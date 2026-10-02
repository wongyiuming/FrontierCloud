#!/usr/bin/env bash
# Isolated real-Nginx routing checks; never toggle a deployed node's data root.
set -euo pipefail
cd "$(dirname "$0")/.."
prefix="fc-native-gate-$(date +%s)-$$"
container="$prefix-nginx"
volume="$prefix-data"
cleanup() {
    docker rm -fv "$container" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker volume create "$volume" >/dev/null
docker run -d --name "$container" --entrypoint nginx \
    -v "$PWD/tests/nginx-maintenance.conf:/etc/nginx/nginx.conf:ro" \
    -v "$PWD/nginx/maintenance-gate.conf:/etc/nginx/maintenance-gate.conf:ro" \
    -v "$PWD/nginx/maintenance.html:/usr/share/nginx/html/maintenance.html:ro" \
    -v "$volume:/app/data" nginx:1.30.4-alpine -g 'daemon off;' >/dev/null
docker exec "$container" nginx -t
ready=false
for _ in $(seq 1 20); do
    if docker exec "$container" wget -q -O /dev/null http://127.0.0.1:8080/health/live; then ready=true; break; fi
    sleep 1
done
if [ "$ready" != true ]; then docker logs --tail 20 "$container"; exit 1; fi
check() {
    local route="$1" expected="$2" response
    response=$(docker exec "$container" wget -S -O /dev/null "http://127.0.0.1:8080$route" 2>&1 || true)
    if ! printf '%s\n' "$response" | grep -q "HTTP/1.1 $expected "; then
        printf 'FAIL: route %s expected %s\n%s\n' "$route" "$expected" "$response"
        exit 1
    fi
}
check /media/stream 200
# These writes are only zero-byte gate fixtures inside our exact fresh volume.
docker exec "$container" touch /app/data/.frontiercloud-maintenance
check /media/stream 503
check /api/v1/media/admin/status 200
docker exec "$container" touch /app/data/.frontiercloud-force-open
check /media/stream 200
docker exec "$container" touch /app/data/.frontiercloud-native-maintenance
for route in /media/stream /api/v1/media/admin/status /internal/v1/cluster-update/status /health/ready /static/file.js; do
    check "$route" 503
done
# A fixed internal error page must render once, not recurse or admit business.
response=$(docker exec "$container" sh -c "printf 'GET /media/stream HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' | nc -w 3 127.0.0.1 8080")
printf '%s\n' "$response" | grep -q '前沿娱乐'
printf '%s\n' 'PASS: native Nginx gate defeats force-open and Admin/control exemptions'
