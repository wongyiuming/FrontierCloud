#!/bin/sh
set -eu

repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$repo_root"

if [ "$(git branch --show-current)" != "dev" ]; then
    echo "RN deployment refused: checkout must be dev" >&2
    exit 1
fi

docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 240
docker compose exec -T nginx nginx -t
server_name="$(docker compose exec -T nginx printenv SERVER_NAME)"
health_attempt=0
until curl -kfsS --resolve "$server_name:443:127.0.0.1" \
    "https://$server_name/api/v1/health" | grep '"status":"healthy"'; do
    health_attempt=$((health_attempt + 1))
    if [ "$health_attempt" -ge 30 ]; then
        echo "RN deployment failed: HTTPS health check did not stabilize" >&2
        exit 1
    fi
    sleep 2
done
