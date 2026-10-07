#!/usr/bin/env bash
# Fresh private-CA fleet; never reuse deployed containers, ports or volumes.
set -euo pipefail
cd "$(dirname "$0")/.."
revision="${FRONTIERCLOUD_REVISION:-$(git rev-parse HEAD)}"
if ! [[ "$revision" =~ ^[0-9a-f]{40}$ ]]; then exit 1; fi
work=$(mktemp -d /tmp/fc-native-matrix-XXXXXXXX)
prefix="fc-matrix-$(cat /proc/sys/kernel/random/uuid)"
image_prefix="${FRONTIERCLOUD_TEST_NATIVE_MATRIX_IMAGE_PREFIX:-$prefix}"
if ! [[ "$image_prefix" =~ ^fc-matrix-[0-9a-f-]{36}$ ]]; then exit 1; fi
native="frontiercloud-native-test:$image_prefix"
agent="frontiercloud-native-agent-test:$image_prefix"
edge="frontiercloud-edge-test:$image_prefix"
driver="frontiercloud-native-driver-test:$image_prefix"
# Private images are retained on failure for diagnosis; no global pruning.
if [[ -z "${FRONTIERCLOUD_TEST_NATIVE_MATRIX_IMAGE_PREFIX:-}" ]]; then
  docker build -f Dockerfile.gin --target build -t "$driver" .
  docker build -f Dockerfile.gin --build-arg "REVISION=$revision" -t "$native" .
  docker build -f updater/Dockerfile.gin --build-arg "REVISION=$revision" -t "$agent" .
  docker build -f nginx/Dockerfile --build-arg "REVISION=$revision" -t "$edge" .
else
  # Only for test-only edits against explicitly selected prior private images.
  docker image inspect "$driver" >/dev/null
  for item in "$native" "$agent" "$edge"; do
    test "$(docker image inspect --format '{{index .Config.Labels "frontiercloud.revision"}}' "$item")" = "$revision"
  done
fi
# Engine Create does not pull images. Provision the shared fixtures explicitly
# so a fresh runner behaves like a development engine with cached images.
docker pull mysql:8.4.11
docker pull redis:7.4.11-alpine
docker run --rm --name "$prefix-driver" --label "frontiercloud.acceptance=$prefix" \
  -v "$PWD:/src:ro" -v "$work:$work" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e FRONTIERCLOUD_TEST_DOCKER_SOCKET=/var/run/docker.sock \
  -e FRONTIERCLOUD_TEST_NATIVE_MATRIX_WORKSPACE="$work" \
  -e FRONTIERCLOUD_TEST_NATIVE_MATRIX_PREFIX="$prefix" \
  -e FRONTIERCLOUD_TEST_NATIVE_MATRIX_MASTER="${FRONTIERCLOUD_TEST_NATIVE_MATRIX_MASTER:-}" \
  -e FRONTIERCLOUD_TEST_NATIVE_MATRIX_NATIVE_IMAGE="$native" \
  -e FRONTIERCLOUD_TEST_NATIVE_MATRIX_AGENT_IMAGE="$agent" \
  -e FRONTIERCLOUD_TEST_NATIVE_MATRIX_EDGE_IMAGE="$edge" \
  "$driver" \
  go test -timeout 50m -count=1 -v ./internal/updater -run TestRealNativeMatrixFleetControl
