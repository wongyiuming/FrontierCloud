#!/usr/bin/env bash
# Isolated optional-MySQL and cold recovery acceptance on a Linux Docker host.
set -euo pipefail
cd "$(dirname "$0")/.."
test "$(id -u)" = 0
evidence="${FC_EVIDENCE_DIR:?Set the external acceptance evidence directory}"
suffix="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
network="fc-final-db-$suffix"
database="fc-final-mysql-$suffix"
cleanup() {
  # Only this UUID-named fixture owns its anonymous MySQL data volume.
  docker rm -fv "$database" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Product images stay clean; this alias and child contain only test tools.
docker tag frontiercloud-python-unit-tools frontiercloud-python-archive:db-test-tools
docker build -t frontiercloud-python-archive:endpoint-tools -f tests/Dockerfile.operator-fixture .
docker network create "$network" >/dev/null
docker run -d --name "$database" --network "$network" --network-alias mysql \
  --cpus 1 --memory 768m -e MYSQL_ALLOW_EMPTY_PASSWORD=yes \
  -e MYSQL_DATABASE=frontiercloud_db_test mysql:8.4.11 >/dev/null
for attempt in $(seq 1 60); do
  if docker exec "$database" mysqladmin ping -h127.0.0.1 --protocol=TCP -uroot --silent >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$database" mysqladmin ping -h127.0.0.1 --protocol=TCP -uroot --silent >/dev/null
docker run --rm --network "$network" --cpus 1 --memory 768m --user 0 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --tmpfs /tmp:size=512m --tmpfs /workspace/data:size=2g,mode=1777 --workdir /workspace \
  -e DB_TYPE=mysql -e FC_DB_MYSQL_RUNTIME=1 \
  -e FC_DB_MYSQL_URL=mysql+asyncmy://root@mysql:3306/frontiercloud_db_test \
  frontiercloud-python-unit-tools python -m unittest tests.test_database_mysql_runtime -v \
  > "$evidence/mysql.log" 2>&1
export FC_OPERATOR_BACKUP_EVIDENCE="$evidence"
FC_OPERATOR_BACKUP_RUNTIME=1 python3 -m tests.operator_backup_runtime -v \
  > "$evidence/operator-backup.log" 2>&1
FC_OPERATOR_ENDPOINT_RUNTIME=1 python3 -m tests.operator_storage_endpoint_runtime -v \
  > "$evidence/operator-endpoint.log" 2>&1
printf '%s\n' 'Optional MySQL, cold backup/restore and identity-preserving endpoint acceptance passed'
