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

monitoring_root="$repo_root/monitoring"
production_env="$monitoring_root/.env"
self_env="$monitoring_root/rn-self.env"

if [ ! -f "$production_env" ] || [ ! -f "$self_env" ]; then
    echo "RN deployment refused: monitoring/.env and monitoring/rn-self.env are required" >&2
    exit 1
fi

cd "$monitoring_root"
python3 render_config.py --env-file "$production_env" --runtime-dir "$monitoring_root"

self_runtime="$monitoring_root/instances/rn-self"
python3 render_config.py --env-file "$self_env" --runtime-dir "$self_runtime"
MONITORING_RUNTIME_DIR=./instances/rn-self \
    docker compose --env-file "$self_env" -p frontiercloud-rn-self-monitoring config --quiet
MONITORING_RUNTIME_DIR=./instances/rn-self \
    docker compose --env-file "$self_env" -p frontiercloud-rn-self-monitoring up -d --wait

COMPOSE_PROFILES=reporting \
    docker compose --env-file "$production_env" -p monitoring config --quiet
COMPOSE_PROFILES=reporting \
    docker compose --env-file "$production_env" -p monitoring up -d --build --wait

docker compose --env-file "$production_env" -p monitoring exec -T weekly_reporter \
    python weekly_report.py --once --dry-run >/dev/null

docker compose --env-file "$production_env" -p monitoring ps
MONITORING_RUNTIME_DIR=./instances/rn-self \
    docker compose --env-file "$self_env" -p frontiercloud-rn-self-monitoring ps
