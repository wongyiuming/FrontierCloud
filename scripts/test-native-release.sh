#!/usr/bin/env bash
# Private five-node whole-release gate. Never reuse deployed containers or data.
set -euo pipefail
cd "$(dirname "$0")/.."
work=$(mktemp -d /tmp/fc-native-matrix-XXXXXXXX)
prefix="fc-matrix-$(cat /proc/sys/kernel/random/uuid)"
driver="frontiercloud-native-driver-test:$prefix"
printf 'Private acceptance workspace: %s\nPrivate acceptance project: %s\n' "$work" "$prefix"
docker build -f Dockerfile.gin --target build -t "$driver" .
# The fleet creates these fixtures through the Engine API, which does not pull.
docker pull mysql:8.4.11
docker pull redis:7.4.11-alpine
docker run --rm -e GOMAXPROCS=2 --name "$prefix-driver" \
  --label "frontiercloud.acceptance=$prefix" \
  --label "frontiercloud.updater-acceptance=$work" \
  -v "$PWD:/src:ro" -v "$work:$work" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e FRONTIERCLOUD_TEST_DOCKER_SOCKET=/var/run/docker.sock \
  -e FRONTIERCLOUD_TEST_NATIVE_MATRIX_WORKSPACE="$work" \
  -e FRONTIERCLOUD_TEST_NATIVE_MATRIX_PREFIX="$prefix" \
  -e FRONTIERCLOUD_TEST_NATIVE_MATRIX_RELEASE=1 \
  -e FRONTIERCLOUD_TEST_NATIVE_MATRIX_MASTER="${FRONTIERCLOUD_TEST_NATIVE_MATRIX_MASTER:-}" \
  "$driver" \
  go test -p=1 -timeout 95m -count=1 -v ./internal/updater -run TestRealNativeMatrixFleetControl
