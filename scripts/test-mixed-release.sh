#!/usr/bin/env bash
# Private ten-node whole-release gate. Never reuse deployed containers or data.
set -euo pipefail
cd "$(dirname "$0")/.."
work=$(mktemp -d /tmp/fc-mixed-runtime-XXXXXXXX)
prefix="fc-mixed-$(cat /proc/sys/kernel/random/uuid)"
driver="frontiercloud-native-driver-test:$prefix"
reference_agent="frontiercloud-reference-agent-test:$prefix"
printf 'Private acceptance workspace: %s\nPrivate acceptance project: %s\n' "$work" "$prefix"
docker build -f Dockerfile.gin --target build -t "$driver" .
docker build -f updater/Dockerfile -t "$reference_agent" .
docker run --rm -e GOMAXPROCS=2 --name "$prefix-driver" \
  --label "frontiercloud.acceptance=$prefix" \
  --label "frontiercloud.updater-acceptance=$work" \
  -v "$PWD:/src:ro" -v "$work:$work" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e FRONTIERCLOUD_TEST_DOCKER_SOCKET=/var/run/docker.sock \
  -e FRONTIERCLOUD_TEST_MIXED_WORKSPACE="$work" \
  -e FRONTIERCLOUD_TEST_MIXED_PREFIX="$prefix" \
  -e FRONTIERCLOUD_TEST_MIXED_RELEASE=1 \
  -e FRONTIERCLOUD_TEST_MIXED_MASTER="${FRONTIERCLOUD_TEST_MIXED_MASTER:-}" \
  -e FRONTIERCLOUD_TEST_MIXED_REFERENCE_AGENT_IMAGE="$reference_agent" \
  "$driver" \
  go test -p=1 -timeout 95m -count=1 -v ./internal/updater -run TestRealMixedRuntimeFleetControl
