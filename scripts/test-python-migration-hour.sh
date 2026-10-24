#!/usr/bin/env bash
# Extended native gate: real cleanup grace, ordinary trusted TLS and live reader.
set -euo pipefail
cd "$(dirname "$0")/.."
test "$(id -u)" = 0
test -z "$(git status --porcelain --untracked-files=all)"
source_sha="$(git rev-parse HEAD)"
evidence="${FC_EVIDENCE_DIR:?Set an absolute evidence directory outside this checkout}"
case "$evidence" in /*) ;; *) echo 'Evidence path must be absolute' >&2; exit 1 ;; esac
evidence="$(realpath -m -- "$evidence")"
case "$evidence/" in "$PWD/"*) echo 'Evidence must be outside this checkout' >&2; exit 1 ;; esac
mkdir -p "$evidence"
chmod 0700 "$evidence"
suffix="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
container="fc-python-migration-hour-$suffix"
cleanup() { docker rm -f "$container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
# The ordinary native gate builds this test-only image. No product image gains
# browser tools, host controls or this fixture's CA.
docker image inspect frontiercloud-python-unit-tools >/dev/null
test ! -L data
mkdir -p data
printf '%s\n' "$source_sha" > "$evidence/migration-hour-source-commit.txt"
docker run --rm --name "$container" --cpus 1 --memory 512m --network none --user 0 \
  --add-host node.4399.com:127.0.0.1 --add-host wrong.4399.com:127.0.0.1 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=sqlite -e FRONTIERCLOUD_PUBLISH_NATIVE=1 \
  -e FRONTIERCLOUD_CUTOVER_NATIVE=1 -e FRONTIERCLOUD_CLEANUP_NATIVE=1 \
  frontiercloud-python-unit-tools timeout --kill-after=10s 4100s \
  python -m tests.resource_transfer_native \
  > "$evidence/migration-hour.json" 2> "$evidence/migration-hour.log"
test "$(git rev-parse HEAD)" = "$source_sha"
test -z "$(git status --porcelain --untracked-files=all)"
cat "$evidence/migration-hour.json"
