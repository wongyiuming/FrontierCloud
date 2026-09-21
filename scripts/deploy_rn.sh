#!/bin/sh
set -eu

repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$repo_root"

image_override="${1:-}"
compose() {
    if [ -n "$image_override" ]; then
        docker compose -f docker-compose.yaml -f "$image_override" "$@"
    else
        docker compose "$@"
    fi
}

if [ "$(git branch --show-current)" != "dev" ]; then
    echo "RN deployment refused: checkout must be dev" >&2
    exit 1
fi

# Remove the retired public setting without printing its secret value.
if [ -f .env ] && grep -q '^[[:space:]]*METRICS_TOKEN[[:space:]]*=' .env; then
    sed -i '/^[[:space:]]*METRICS_TOKEN[[:space:]]*=/d' .env
    echo "Removed obsolete METRICS_TOKEN from RN .env"
fi

python3 scripts/validate_env_contract.py
compose config --quiet
if [ -n "$image_override" ]; then
    test -f "$image_override"
    compose up -d --no-build --remove-orphans --wait --wait-timeout 45
else
    compose up -d --build --remove-orphans --wait --wait-timeout 240
fi
compose exec -T nginx nginx -t
tls_enabled="$(compose exec -T nginx printenv TLS_ENABLED)"
if [ "$tls_enabled" != "true" ]; then
    echo "RN deployment failed: TLS_ENABLED must be true" >&2
    exit 1
fi
server_name="$(compose exec -T nginx printenv SERVER_NAME)"
health_attempt=0
until curl -fsS --resolve "$server_name:443:127.0.0.1" \
    "https://$server_name/health/ready" | grep '"status":"ready"'; do
    health_attempt=$((health_attempt + 1))
    if [ "$health_attempt" -ge 30 ]; then
        echo "RN deployment failed: HTTPS health check did not stabilize" >&2
        exit 1
    fi
    sleep 2
done
