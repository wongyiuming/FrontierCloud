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
    echo "FrontierCloud deployment refused: checkout must be dev" >&2
    exit 1
fi

# Remove the retired public setting without printing its secret value.
if [ -f .env ] && grep -q '^[[:space:]]*METRICS_TOKEN[[:space:]]*=' .env; then
    sed -i '/^[[:space:]]*METRICS_TOKEN[[:space:]]*=/d' .env
    echo "Removed obsolete METRICS_TOKEN from deployment .env"
fi

python3 scripts/validate_env_contract.py
compose config --quiet

expected_schema_generation="$(sed -n 's/^SCHEMA_GENERATION = \([0-9][0-9]*\)$/\1/p' app/core/db.py)"
case "$expected_schema_generation" in
    ''|*[!0-9]*)
        echo "FrontierCloud deployment failed: invalid SCHEMA_GENERATION" >&2
        exit 1
        ;;
esac

# dev CD targets are disposable node instances. The application never migrates an
# existing database: when the tested commit requires a different initialization
# generation, CD performs an explicit fresh-node database initialization instead.
if [ -n "$image_override" ]; then
    compose up -d --no-build secrets-init mysql redis --wait --wait-timeout 45
else
    compose up -d --build secrets-init mysql redis --wait --wait-timeout 240
fi

table_count="$(compose exec -T mysql sh -c '
    MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)" \
    mysql -N -B -u"$MYSQL_USER" "$MYSQL_DATABASE" -e \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND TABLE_TYPE=\"BASE TABLE\""
')"

fresh_init=false
if [ "$table_count" -gt 0 ]; then
    marker_count="$(compose exec -T mysql sh -c '
        MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)" \
        mysql -N -B -u"$MYSQL_USER" "$MYSQL_DATABASE" -e \
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=\"frontiercloud_schema\""
    ')"
    if [ "$marker_count" -eq 1 ]; then
        current_schema_generation="$(compose exec -T mysql sh -c '
            MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)" \
            mysql -N -B -u"$MYSQL_USER" "$MYSQL_DATABASE" -e \
            "SELECT generation FROM frontiercloud_schema WHERE singleton=1"
        ')"
    else
        current_schema_generation="legacy"
    fi
    if [ "$current_schema_generation" != "$expected_schema_generation" ]; then
        fresh_init=true
        echo "FrontierCloud dev CD: schema generation ${current_schema_generation} is incompatible with ${expected_schema_generation}; performing explicit fresh-node init"
    fi
fi

if [ "$fresh_init" = true ]; then
    compose stop web nginx >/dev/null 2>&1 || true
    compose exec -T mysql sh -c '
        case "$MYSQL_DATABASE" in
            ""|*[!A-Za-z0-9_]*)
                echo "Unsafe MYSQL_DATABASE for fresh-node init" >&2
                exit 1
                ;;
        esac
        MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_root_password)" \
        mysql -uroot -e \
        "DROP DATABASE IF EXISTS \`$MYSQL_DATABASE\`; CREATE DATABASE \`$MYSQL_DATABASE\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
    '
    compose exec -T redis redis-cli FLUSHDB >/dev/null
fi

if [ -n "$image_override" ]; then
    test -f "$image_override"
    compose up -d --no-build --remove-orphans --wait --wait-timeout 45
else
    compose up -d --build --remove-orphans --wait --wait-timeout 240
fi
compose exec -T nginx nginx -t
tls_enabled="$(compose exec -T nginx printenv TLS_ENABLED)"
if [ "$tls_enabled" != "true" ]; then
    echo "FrontierCloud deployment failed: TLS_ENABLED must be true" >&2
    exit 1
fi
server_name="$(compose exec -T nginx printenv SERVER_NAME)"
health_attempt=0
until curl -fsS --resolve "$server_name:443:127.0.0.1" \
    "https://$server_name/health/ready" | grep '"status":"ready"'; do
    health_attempt=$((health_attempt + 1))
    if [ "$health_attempt" -ge 30 ]; then
        echo "FrontierCloud deployment failed: HTTPS health check did not stabilize" >&2
        exit 1
    fi
    sleep 2
done
