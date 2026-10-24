#!/usr/bin/env bash
# Complete native acceptance on an isolated Docker development host.
set -euo pipefail
cd "$(dirname "$0")/.."
test -z "$(git status --porcelain --untracked-files=all)"
source_sha="$(git rev-parse HEAD)"
evidence="${FC_EVIDENCE_DIR:?Set an absolute evidence directory outside this checkout}"
browser_python="${FC_BROWSER_PYTHON:-python3}"
command -v certutil >/dev/null
case "$evidence" in /*) ;; *) echo 'Evidence path must be absolute' >&2; exit 1 ;; esac
mkdir -p "$evidence"
chmod 0700 "$evidence"
export FRONTIERCLOUD_CERTIFICATE_EVIDENCE="$evidence"
python3 -m tests.compose_layout_native > "$evidence/compose-layout.json"
docker build -t frontiercloud-acceptance-web .
docker build -t frontiercloud-acceptance-nginx -f nginx/Dockerfile .
tools_context="$(mktemp -d /tmp/fc-python-unit-tools.XXXXXXXX)"
unit_names=()
sqlite_hour_pid=''
gui_pid=''
success_hour_pid=''
cleanup() {
  local join_pids=()
  # Signal only jobs still tracked by this shell, then join owned containers.
  for fixture_pid in "$gui_pid" "$sqlite_hour_pid" "$success_hour_pid"; do
    for running_pid in $(jobs -pr); do
      if test "$fixture_pid" = "$running_pid"; then
        kill -TERM "$fixture_pid" >/dev/null 2>&1 || true
        join_pids+=("$fixture_pid")
      fi
    done
  done
  if ((${#unit_names[@]})); then docker rm -f "${unit_names[@]}" >/dev/null 2>&1 || true; fi
  for fixture_pid in "${join_pids[@]}"; do wait "$fixture_pid" >/dev/null 2>&1 || true; done
  case "$tools_context" in /tmp/fc-python-unit-tools.*) rm -rf -- "$tools_context" ;; *) exit 1 ;; esac
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cat > "$tools_context/Dockerfile" <<'EOF'
FROM frontiercloud-acceptance-web
USER 0
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs && rm -rf /var/lib/apt/lists/* && \
    git config --system --add safe.directory /workspace
EOF
docker build -t frontiercloud-python-unit-tools "$tools_context"
python3 -m tests.native_runner_cleanup --tools-image frontiercloud-python-unit-tools \
  --output "$evidence/native-runner-cleanup" > "$evidence/native-runner-cleanup.log" 2>&1
install -m 0600 "$evidence/native-runner-cleanup/result.json" "$evidence/native-runner-cleanup.json"
# A clean Git clone has no ignored data directory; runc needs its mountpoint
# before placing disposable tmpfs over the read-only source bind.
test ! -L data
mkdir -p data
mkdir -p "$evidence/unit-shards"
unit_token="$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
unit_pids=()
for shard in 0 1 2 3; do
  unit_names+=("fc-unit-$unit_token-$shard")
  docker run --rm --name "fc-unit-$unit_token-$shard" --cpus 1 --memory 512m --network none --user 0 \
    --mount "type=bind,source=$PWD,target=/workspace,readonly" \
    --mount "type=bind,source=$evidence/unit-shards,target=/unit-evidence" \
    --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
    --workdir /workspace -e PYTHONDONTWRITEBYTECODE=1 frontiercloud-python-unit-tools \
    python scripts/test_python_units.py run --count 4 --index "$shard" \
      --receipt "/unit-evidence/shard-$shard.json" > "$evidence/unit-shards/shard-$shard.log" 2>&1 &
  unit_pids+=("$!")
done
unit_failed=0
for unit_pid in "${unit_pids[@]}"; do
  if ! wait "$unit_pid"; then unit_failed=1; fi
done
cat "$evidence"/unit-shards/shard-*.log > "$evidence/unit.log"
tail -8 "$evidence/unit.log"
test "$unit_failed" = 0
docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --mount "type=bind,source=$evidence/unit-shards,target=/unit-evidence" \
  --mount "type=bind,source=$evidence,target=/evidence" \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  --workdir /workspace -e PYTHONDONTWRITEBYTECODE=1 frontiercloud-python-unit-tools \
  python scripts/test_python_units.py verify --count 4 \
    --receipts /unit-evidence/shard-{0,1,2,3}.json --receipt /evidence/unit-coverage.json
python3 scripts/check_cpu_boundary.py
python3 scripts/validate_env_contract.py
python3 scripts/check_release_policy.py
python3 scripts/check_english_comments.py
find static/js -name '*.js' -print0 | xargs -0 -n1 node --check
node tests/admin_ui_smoke.mjs
node tests/admin_upload_transport_smoke.mjs
node tests/player_cache_smoke.mjs
node tests/audio_continuous_stream_smoke.mjs
node tests/audio_continuous_fetch_retry_smoke.mjs
node tests/music_metadata_smoke.mjs
node tests/s3_upload_smoke.mjs
node tests/business_navigation_smoke.mjs
node tests/directory_globe_smoke.mjs
node tests/media_crypto_smoke.mjs
FC_CRYPTO_COMPILED=1 node tests/media_crypto_smoke.mjs
node tests/network_observation_smoke.mjs

# Native touch negotiation uses the prepared browser runtime before long fixtures.
timeout --kill-after=10s 180s "$browser_python" -B tests/player_touch_seek_browser.py \
  --output "$evidence/private/player-touch-seek" > "$evidence/player-touch-seek.log" 2>&1
docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=512m,mode=1777 \
  --workdir /workspace -e PYTHONDONTWRITEBYTECODE=1 \
  frontiercloud-python-unit-tools timeout --kill-after=5s 90s python -m tests.resource_inventory_native \
  > "$evidence/native-inventory.json" 2> "$evidence/native-inventory.log"

docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=sqlite frontiercloud-python-unit-tools \
  timeout --kill-after=5s 90s python -m tests.node_retirement_runtime_native \
  > "$evidence/native-retirement-runtime.json" 2> "$evidence/native-retirement-runtime.log"

docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=sqlite frontiercloud-python-unit-tools \
  timeout --kill-after=5s 120s python -m tests.node_retirement_api_native \
  > "$evidence/native-retirement-api.json" 2> "$evidence/native-retirement-api.log"
docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  --workdir /workspace -e PYTHONDONTWRITEBYTECODE=1 \
  frontiercloud-python-unit-tools timeout --kill-after=5s 120s python -m tests.resource_planning_native \
  > "$evidence/native-planning.json" 2> "$evidence/native-planning.log"
docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 frontiercloud-python-unit-tools \
  timeout --kill-after=5s 90s python -m tests.resource_reservations_native \
  > "$evidence/native-reservations.json" 2> "$evidence/native-reservations.log"
docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 frontiercloud-python-unit-tools \
  timeout --kill-after=5s 120s python -m tests.resource_admission_native \
  > "$evidence/native-admission.json" 2> "$evidence/native-admission.log"
docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e FRONTIERCLOUD_CANCEL_NATIVE=1 frontiercloud-python-unit-tools \
  timeout --kill-after=5s 180s python -m tests.resource_transfer_native \
  > "$evidence/native-transfer.json" 2> "$evidence/native-transfer.log"
docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e FRONTIERCLOUD_PUBLISH_NATIVE=1 -e FRONTIERCLOUD_CUTOVER_NATIVE=1 frontiercloud-python-unit-tools \
  timeout --kill-after=5s 200s python -m tests.resource_transfer_native \
  > "$evidence/native-publication.json" 2> "$evidence/native-publication.log"
docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e FRONTIERCLOUD_PUBLISH_NATIVE=1 -e FRONTIERCLOUD_SHADOW_CANCEL_NATIVE=1 \
  -e FRONTIERCLOUD_SOURCE_RETAIN_NATIVE=1 -e FRONTIERCLOUD_UNIT_SETTLE_NATIVE=1 frontiercloud-python-unit-tools \
  timeout --kill-after=5s 600s python -m tests.resource_transfer_native \
  > "$evidence/native-unit-cancel.json" 2> "$evidence/native-unit-cancel.log"
docker run --rm --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e FRONTIERCLOUD_PUBLISH_NATIVE=1 -e FRONTIERCLOUD_SHADOW_CANCEL_NATIVE=1 \
  -e FRONTIERCLOUD_CANCEL_MASTER_NATIVE=1 -e FRONTIERCLOUD_CANCEL_CENSUS_NATIVE=1 -e FRONTIERCLOUD_CANCEL_SOURCE_RELEASE_NATIVE=1 \
  -e FRONTIERCLOUD_CANCEL_TARGET_RELEASE_NATIVE=1 -e FRONTIERCLOUD_CANCEL_MASTER_RELEASE_NATIVE=1 frontiercloud-python-unit-tools \
  timeout --kill-after=5s 720s python -m tests.resource_transfer_native \
  > "$evidence/native-master-cancel.json" 2> "$evidence/native-master-cancel.log"
run_sqlite_partial() {
  local pair="$1"
  actual_hour=0
  if [ "$pair" = DR ]; then actual_hour=1; fi
  docker run --rm --name "fc-native-partial-$unit_token-$pair" --cpus 1 --memory 512m --network none --user 0 \
    --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
    --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
    --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
    -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=sqlite -e FC_PARTIAL_TRANSPORT_PAIR="$pair" \
    -e FC_PARTIAL_MASTER_PROCESS=1 -e FC_PARTIAL_ACTUAL_HOUR="$actual_hour" frontiercloud-python-unit-tools \
    timeout --kill-after=5s 4800s python -m tests.resource_cancel_partial_native \
    > "$evidence/native-partial-cancel-$pair.json" 2> "$evidence/native-partial-cancel-$pair.log"
}
for pair in DR DD RD RR; do
  unit_names+=("fc-native-partial-$unit_token-$pair")
  if [ "$pair" = DR ]; then
    (trap - EXIT INT TERM; run_sqlite_partial "$pair") &
    sqlite_hour_pid=$!
  else
    run_sqlite_partial "$pair"
  fi
done
FRONTIERCLOUD_TRAFFIC_EVIDENCE="$evidence/business-traffic" bash scripts/test-python-business-traffic.sh > "$evidence/business-traffic.log" 2>&1
bash scripts/test-python-certificate-nginx.sh > "$evidence/certificate-nginx.log" 2>&1
bash scripts/test-python-certificate-storage.sh > "$evidence/certificate-storage.log" 2>&1
# The fleet checks each single-core Web's CPU recovery through check_cpu_quiescence.py.
"$browser_python" -m tests.federation_stack --output "$evidence/federation.json" > "$evidence/federation.log" 2>&1
FC_NODES_BROWSER_ROOT="$evidence/private/nodes-browser-fixture-native" "$browser_python" -m tests.nodes_browser_native > "$evidence/nodes-browser.log" 2>&1
FC_MIGRATION_UI_EVIDENCE="$evidence/migration-ui" "$browser_python" -m tests.migrations_browser > "$evidence/migration-ui.log" 2>&1
FC_MIGRATION_GUI_TERMINAL=1 FC_MIGRATION_BROWSER_ROOT="$evidence/private/migration-browser-fixture-native" timeout --kill-after=30s 4800s "$browser_python" -m tests.migration_browser_native > "$evidence/migration-browser-native.log" 2>&1 &
gui_pid=$!
printf '%s\n' "$source_sha" > "$evidence/migration-hour-source-commit.txt"
unit_names+=("fc-native-success-hour-$unit_token")
docker run --rm --name "fc-native-success-hour-$unit_token" --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=sqlite -e FRONTIERCLOUD_PUBLISH_NATIVE=1 \
  -e FRONTIERCLOUD_CUTOVER_NATIVE=1 -e FRONTIERCLOUD_CLEANUP_NATIVE=1 \
  frontiercloud-python-unit-tools timeout --kill-after=10s 4100s \
  python -m tests.resource_transfer_native \
  > "$evidence/migration-hour.json" 2> "$evidence/migration-hour.log" &
success_hour_pid=$!
install -m 0600 "$evidence/private/nodes-browser-fixture-native/result.json" "$evidence/nodes-browser.json"
FC_PLAYER_STORAGE_ROOT="$evidence/private/player-storage-fixture-native" timeout --kill-after=30s 1800s "$browser_python" -m tests.player_storage_browser > "$evidence/player-storage-browser.log" 2>&1
install -m 0600 "$evidence/private/player-storage-fixture-native/result.json" "$evidence/player-storage-browser.json"
FC_FIVE_NODE_TRAFFIC=1 FC_PLAYER_STORAGE_ROOT="$evidence/private/player-storage-fixture-five-traffic" timeout --kill-after=30s 1800s "$browser_python" -m tests.player_storage_browser > "$evidence/five-node-traffic.log" 2>&1
install -m 0600 "$evidence/private/player-storage-fixture-five-traffic/result.json" "$evidence/five-node-traffic.json"
FC_FILESYSTEM_UPLOAD_FAULTS=1 FC_PLAYER_STORAGE_ROOT="$evidence/private/player-storage-fixture-upload-faults" timeout --kill-after=30s 1800s "$browser_python" -m tests.player_storage_browser > "$evidence/filesystem-upload-faults.log" 2>&1
install -m 0600 "$evidence/private/player-storage-fixture-upload-faults/result.json" "$evidence/filesystem-upload-faults.json"
FC_S3_LONG_IDLE=1 FC_S3_EVIDENCE_DIR="$evidence/s3" bash scripts/test-python-s3.sh > "$evidence/s3.log" 2>&1
# Cold operator discovery must not overlap any background fixture's teardown.
wait "$sqlite_hour_pid"
sqlite_hour_pid=''
wait "$gui_pid"
gui_pid=''
wait "$success_hour_pid"
success_hour_pid=''
install -m 0600 "$evidence/private/migration-browser-fixture-native/result.json" "$evidence/migration-browser-native.json"
bash scripts/test-python-operator.sh > "$evidence/operator.log" 2>&1
test -z "$(git status --porcelain --untracked-files=all)"
test "$(git rev-parse HEAD)" = "$source_sha"
printf '%s\n' "$source_sha" > "$evidence/source-commit.txt"
printf '%s\n' "Complete native acceptance passed for $source_sha"
