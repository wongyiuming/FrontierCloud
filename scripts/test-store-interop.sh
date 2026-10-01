#!/usr/bin/env bash
# Test only disposable stores; never point this script at a deployed node.
set -euo pipefail
cd "$(dirname "$0")/.."
prefix="fc-store-interop-$(date +%s)-$$"
network="$prefix-network"
secrets="$prefix-secrets"
data="$prefix-data"
mysql="$prefix-mysql"
go_image="frontiercloud-gin:store-test"
python_image="frontiercloud-python:store-test"

cleanup() {
    docker rm -fv "$mysql" >/dev/null 2>&1 || true
    docker volume rm "$secrets" "$data" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker build -f Dockerfile.gin -t "$go_image" .
docker build -f tests/store.Dockerfile -t "$python_image" .
docker run --rm --entrypoint python "$python_image" -m unittest discover -s tests -p test_protocol_conformance.py -q
docker run --rm --entrypoint python "$python_image" -m unittest discover -s tests -p test_sqlite_store.py -q
docker network create "$network" >/dev/null
docker volume create "$secrets" >/dev/null
docker volume create "$data" >/dev/null
docker run --rm --user 0:0 -v "$secrets:/run/frontiercloud-secrets" "$go_image" init-secrets
docker run --rm --user 0:0 -v "$data:/app/data" "$go_image" init-media

go_sqlite() {
    docker run --rm -e DB_TYPE=sqlite -e "SQLITE_PATH=/app/data/$1.db" -v "$data:/app/data" "$go_image" migrate
}
python_sqlite() {
    docker run --rm -e DB_TYPE=sqlite -e "SQLITE_PATH=/app/data/$1.db" -v "$data:/app/data" "$python_image" "${2:-verify}"
}

# Both directions use the same file, durable IDs, and migration journal.
go_sqlite go
python_sqlite go
python_sqlite go generation-one-fixture
go_sqlite go
python_sqlite go
python_sqlite python
go_sqlite python
python_sqlite python generation-one-fixture
python_sqlite python
go_sqlite python

docker run -d --name "$mysql" --network "$network" --network-alias mysql \
    -e MYSQL_DATABASE=office_automation -e MYSQL_USER=media_admin \
    -e MYSQL_PASSWORD_FILE=/run/frontiercloud-secrets/mysql_password \
    -e MYSQL_ROOT_PASSWORD_FILE=/run/frontiercloud-secrets/mysql_root_password \
    -v "$secrets:/run/frontiercloud-secrets:ro" mysql:8.4.11 >/dev/null
ready=false
for _ in $(seq 1 90); do
    if docker exec "$mysql" sh -c 'MYSQL_PWD=$(cat /run/frontiercloud-secrets/mysql_password) mysql -h 127.0.0.1 -u media_admin -e "SELECT 1" office_automation' >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [ "$ready" != true ]; then
    docker logs --tail 40 "$mysql"
    exit 1
fi

go_mysql() {
    docker run --rm --network "$network" -e DB_TYPE=mysql -e "MYSQL_DATABASE=$1" \
        -v "$secrets:/run/frontiercloud-secrets:ro" "$go_image" migrate
}
python_mysql() {
    docker run --rm --network "$network" -e DB_TYPE=mysql -e "MYSQL_DATABASE=$1" \
        -v "$secrets:/run/frontiercloud-secrets:ro" "$python_image" "${2:-verify}"
}

go_mysql office_automation
python_mysql office_automation
python_mysql office_automation generation-one-fixture
go_mysql office_automation
python_mysql office_automation
docker exec "$mysql" sh -c 'MYSQL_PWD=$(cat /run/frontiercloud-secrets/mysql_root_password) mysql -u root -e "CREATE DATABASE fc_python CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; GRANT ALL ON fc_python.* TO '\''media_admin'\''@'\''%'\'';"'
python_mysql fc_python
go_mysql fc_python
python_mysql fc_python generation-one-fixture
python_mysql fc_python
go_mysql fc_python
printf '%s\n' 'PASS: Python/Go x SQLite/MySQL schema initialization, restart, and generation 1 -> 2 interoperability'
