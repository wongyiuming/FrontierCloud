#!/usr/bin/env bash
# Full native self-update in a newly created project, never the host topology.
set -euo pipefail
cd "$(dirname "$0")/.."
# The Go test retires only its randomly labeled containers/images/network.
# Retain this host child on interruption; a surviving bind writer must not have
# its files removed underneath it. On CI the disposable runner owns the child.
for database in sqlite mysql; do
  work=$(mktemp -d /tmp/fc-native-updater-XXXXXXXX)
  printf 'Private native updater fixture (%s): %s\n' "$database" "$work"
  docker run --rm -e GOMAXPROCS=2 --label "frontiercloud.updater-acceptance=$work" -v "$PWD:/src:ro" \
    -v /var/run/docker.sock:/var/run/docker.sock -v "$work:$work" \
    -e FRONTIERCLOUD_TEST_DOCKER_SOCKET=/var/run/docker.sock \
    -e FRONTIERCLOUD_TEST_UPDATER_WORKSPACE="$work" \
    -e FRONTIERCLOUD_TEST_UPDATER_DATABASE="$database" \
    -e FRONTIERCLOUD_TEST_UPDATER_MANIFEST=1 \
    frontiercloud-go:business-test \
    go test -p=1 -race -timeout 60m -count=1 -v ./internal/updater -run 'TestRealNativeUpdater(UpgradeHandoffRollback|WholeManifestHandoffRollback)$'
done
