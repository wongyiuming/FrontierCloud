#!/usr/bin/env bash
# Isolated optional-MySQL and cold recovery acceptance on a Linux Docker host.
set -euo pipefail
cd "$(dirname "$0")/.."
test "$(id -u)" = 0
evidence="${FC_EVIDENCE_DIR:?Set the external acceptance evidence directory}"
suffix="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
network="fc-final-db-$suffix"
database="fc-final-mysql-$suffix"
client="fc-final-mysql-client-$suffix"
cleanup() {
  # Only this UUID-named fixture owns its anonymous MySQL data volume.
  docker rm -fv "$client" >/dev/null 2>&1 || true
  docker rm -fv "$database" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
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
run_mysql_phase() {
docker run --rm --name "$client" --network "$network" --cpus 1 --memory 512m --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --tmpfs /tmp:size=2g --tmpfs /workspace/data:size=2g,mode=1777 --workdir /workspace \
  -e DB_TYPE=mysql -e FC_DB_MYSQL_RUNTIME=1 \
  -e FC_DB_MYSQL_URL=mysql+asyncmy://root@mysql:3306/frontiercloud_db_test \
  frontiercloud-python-unit-tools bash -ec 'python -m tests.resource_fences_mysql_native; python -m tests.resource_reservations_mysql_native; python -m tests.resource_admission_mysql_native; python -m tests.resource_transfer_mysql_native; python -m tests.resource_cancel_mysql_native; python -m tests.resource_cancel_authority_mysql_native; python -m tests.resource_cancel_target_mysql_native; python -m tests.resource_cancel_source_peers_mysql_native; python -m tests.resource_cancel_unit_mysql_native; python -m tests.resource_cancel_master_mysql_native; python -m tests.resource_cancel_cleanup_mysql_native; python -m tests.resource_cancel_census_mysql_native; python -m tests.resource_publish_mysql_native; python -m tests.resource_cutover_mysql_native; python -m tests.resource_target_verify_mysql_native; python -m tests.resource_source_cleanup_mysql_native; python -m tests.resource_finalization_mysql_native; python -m tests.resource_scheduler_mysql_native; python -m tests.resource_migration_admin_mysql_native; python -m tests.resource_encryption_binding_mysql_native; python -m unittest tests.test_database_mysql_runtime -v; python -m tests.node_retirement_mysql_native; python -m tests.node_retirement_files_mysql_native; python -m tests.node_retirement_history_mysql_native; python -m tests.node_retirement_complete_mysql_native; python -m tests.node_retirement_migration_complete_mysql_native; python -m tests.node_retirement_api_mysql_native; python -m tests.s3_members_mysql_native; python -m tests.s3_uploads_mysql_native' \
  > "$evidence/mysql.log" 2>&1

docker run --rm --name "$client" --network "$network" --cpus 1 --memory 512m --user 0 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /tmp:size=2g --tmpfs /workspace/data:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=mysql -e FC_DB_MYSQL_RUNTIME=1 \
  -e FC_DB_MYSQL_URL=mysql+asyncmy://root@mysql:3306/frontiercloud_db_test \
  frontiercloud-python-unit-tools python -m unittest -v tests.s3_objects_mysql_native tests.s3_admin_mysql_native \
  >> "$evidence/mysql.log" 2>&1
docker run --rm --name "$client" --network "$network" --cpus 1 --memory 512m --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /tmp:size=2g --tmpfs /workspace/data:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=mysql -e FC_DB_MYSQL_RUNTIME=1 \
  -e FC_DB_MYSQL_URL=mysql+asyncmy://root@mysql:3306/frontiercloud_db_test \
  frontiercloud-python-unit-tools timeout --kill-after=5s 120s python -m tests.node_retirement_api_https_mysql_native \
  > "$evidence/retirement-api-https-mysql.json" 2> "$evidence/retirement-api-https-mysql.log"
docker run --rm --name "$client" --cpus 1 --memory 512m --network "$network" --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=mysql -e FC_DB_MYSQL_RUNTIME=1 \
  -e FC_DB_MYSQL_URL=mysql+asyncmy://root@mysql:3306/frontiercloud_db_test \
  frontiercloud-python-unit-tools timeout --kill-after=5s 600s python -m tests.resource_cancel_unit_https_mysql_native \
  > "$evidence/native-unit-cancel-mysql.jsonl" 2> "$evidence/native-unit-cancel-mysql.log"
docker run --rm --name "$client" --cpus 1 --memory 512m --network "$network" --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=mysql -e FC_DB_MYSQL_RUNTIME=1 \
  -e FC_DB_MYSQL_URL=mysql+asyncmy://root@mysql:3306/frontiercloud_db_test \
  frontiercloud-python-unit-tools timeout --kill-after=5s 720s python -m tests.resource_cancel_census_https_mysql_native \
  > "$evidence/native-master-cancel-mysql.jsonl" 2> "$evidence/native-master-cancel-mysql.log"
for pair in DR DD RD RR; do
  actual_hour=0
  if [ "$pair" = DR ]; then actual_hour=1; fi
  docker run --rm --name "$client" --cpus 1 --memory 512m --network "$network" --user 0 \
    --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
    --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
    --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
    -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=mysql -e FC_DB_MYSQL_RUNTIME=1 \
    -e FC_DB_MYSQL_URL=mysql+asyncmy://root@mysql:3306/frontiercloud_db_test \
    -e FC_PARTIAL_TRANSPORT_PAIR="$pair" -e FC_PARTIAL_MASTER_PROCESS=1 -e FC_PARTIAL_ACTUAL_HOUR="$actual_hour" \
    frontiercloud-python-unit-tools timeout --kill-after=5s 4800s python -m tests.resource_cancel_partial_https_mysql_native \
    > "$evidence/native-partial-cancel-mysql-$pair.jsonl" 2> "$evidence/native-partial-cancel-mysql-$pair.log"
done
}
run_mysql_phase
FC_OPERATOR_DISCOVERY_EVIDENCE="$evidence/operator-discovery-inputs.jsonl" \
 FC_OPERATOR_DISCOVERY_RUNTIME=1 python3 -m tests.operator_deployment_native \
  > "$evidence/operator-discovery.log" 2>&1
install -d -m 0700 "$evidence/private/operator-prepare"
FC_OPERATOR_DISCOVERY_EVIDENCE="$evidence/operator-prepare-inputs.jsonl" \
 FC_OPERATOR_PREPARE_RUNTIME=1 FC_OPERATOR_PREPARE_EVIDENCE="$evidence/private/operator-prepare" \
 python3 -m tests.operator_prepare_native > "$evidence/operator-prepare.log" 2>&1
install -d -m 0700 "$evidence/private/operator-application"
install -d -m 0700 "$evidence/private/operator-install"
FC_OPERATOR_INSTALL_RUNTIME=1 FC_OPERATOR_INSTALL_EVIDENCE="$evidence/private/operator-install" \
 python3 -m unittest -v tests.operator_install_native > "$evidence/operator-install.log" 2>&1
FC_OPERATOR_DISCOVERY_EVIDENCE="$evidence/operator-application-inputs.jsonl" \
 FC_OPERATOR_APPLICATION_RUNTIME=1 FC_OPERATOR_APPLICATION_EVIDENCE="$evidence/private/operator-application" \
 python3 -m unittest -v tests.operator_application_native > "$evidence/operator-application.log" 2>&1
export FC_OPERATOR_BACKUP_EVIDENCE="$evidence"
FC_OPERATOR_BACKUP_RUNTIME=1 python3 -m tests.operator_backup_runtime -v \
  > "$evidence/operator-backup.log" 2>&1
FC_OPERATOR_ENDPOINT_RUNTIME=1 python3 -m tests.operator_storage_endpoint_runtime -v \
  > "$evidence/operator-endpoint.log" 2>&1
printf '%s\n' 'Optional MySQL, application upgrade, fenced recovery preparation, cold backup/restore and endpoint acceptance passed'
