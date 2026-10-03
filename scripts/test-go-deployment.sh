#!/usr/bin/env bash
# Parse the real isolated native deployment; never start deployed services.
set -euo pipefail
cd "$(dirname "$0")/.."
export FRONTIERCLOUD_REVISION="${FRONTIERCLOUD_REVISION:-$(git rev-parse HEAD)}"
if ! [[ "$FRONTIERCLOUD_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s\n' 'Exact committed native image revision required' >&2
    exit 1
fi
work=$(mktemp -d /tmp/fc-go-deployment-XXXXXXXX)
cleanup() {
    # Only this generated parser-output child, never a deployment data root.
    if [[ "$work" == /tmp/fc-go-deployment-* && ! -L "$work" && -d "$work" ]]; then
        rm -r -- "$work"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export COMPOSE_PROJECT_NAME="fc-go-config-$$"
export DB_TYPE=sqlite SQLITE_PATH=/app/data/frontiercloud.db
docker compose --env-file /dev/null -f docker-compose.gin.yaml config --format json > "$work/sqlite.json"
docker compose --env-file /dev/null -f docker-compose.gin.yaml -f docker-compose.gin-mysql.yaml config --format json > "$work/mysql.json"
docker run --rm -v "$PWD:/src:ro" -v "$work:/compose-check:ro" \
    -e FRONTIERCLOUD_TEST_COMPOSE_JSON_DIR=/compose-check \
    frontiercloud-go:business-test \
    go test -count=1 -v ./internal/deployment
printf '%s\n' 'PASS: actual native SQLite/MySQL Compose selection'
