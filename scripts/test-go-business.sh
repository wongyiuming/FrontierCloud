#!/usr/bin/env bash
# Isolated real-driver tests. This script must never use a deployed database.
set -euo pipefail
cd "$(dirname "$0")/.."
prefix="fc-go-business-$(date +%s)-$$"
network="$prefix-network"
secrets="$prefix-secrets"
mysql="$prefix-mysql"
redis="$prefix-redis"
build_image="frontiercloud-go:business-test"
runtime_image="frontiercloud-gin:business-test"
cleanup() {
    docker rm -fv "$mysql" "$redis" >/dev/null 2>&1 || true
    docker volume rm "$secrets" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker build -f Dockerfile.gin --target build -t "$build_image" .
docker build -f Dockerfile.gin -t "$runtime_image" .
docker network create "$network" >/dev/null
docker volume create "$secrets" >/dev/null
docker run --rm --user 0:0 -v "$secrets:/run/frontiercloud-secrets" "$runtime_image" init-secrets
docker run -d --name "$mysql" --network "$network" --network-alias mysql \
    -e MYSQL_DATABASE=fc_business -e MYSQL_USER=media_admin \
    -e MYSQL_PASSWORD_FILE=/run/frontiercloud-secrets/mysql_password \
    -e MYSQL_ROOT_PASSWORD_FILE=/run/frontiercloud-secrets/mysql_root_password \
    -v "$secrets:/run/frontiercloud-secrets:ro" mysql:8.4.11 >/dev/null
docker run -d --name "$redis" --network "$network" --network-alias redis redis:7.4.11-alpine >/dev/null
ready=false
for _ in $(seq 1 90); do
    if docker exec "$mysql" sh -c 'MYSQL_PWD=$(cat /run/frontiercloud-secrets/mysql_password) mysql -h 127.0.0.1 -u media_admin -e "SELECT 1" fc_business' >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [ "$ready" != true ]; then docker logs --tail 40 "$mysql"; exit 1; fi
docker run --rm --network "$network" -v "$secrets:/run/frontiercloud-secrets:ro" \
    -e FRONTIERCLOUD_TEST_MYSQL_HOST=mysql -e FRONTIERCLOUD_TEST_MYSQL_DATABASE=fc_business \
    -e FRONTIERCLOUD_TEST_MYSQL_USER=media_admin \
    -e FRONTIERCLOUD_TEST_MYSQL_PASSWORD_FILE=/run/frontiercloud-secrets/mysql_password \
    "$build_image" go test -count=1 -v ./internal/store/business
docker run --rm --network "$network" -e FRONTIERCLOUD_TEST_REDIS_URL=redis://redis:6379/1 \
    "$build_image" go test -count=1 -v ./internal/admin
docker run --rm --network "$network" -e FRONTIERCLOUD_TEST_REDIS_URL=redis://redis:6379/2 \
    "$build_image" go test -count=1 -v ./internal/httpapi
docker run --rm --network "$network" -e FRONTIERCLOUD_TEST_REDIS_URL=redis://redis:6379/3 \
    "$build_image" go test -count=1 -v ./internal/observation
docker run --rm "$build_image" go test -race ./...
printf '%s\n' 'PASS: real MySQL media transactions, Redis Admin authentication and Go race checks'
