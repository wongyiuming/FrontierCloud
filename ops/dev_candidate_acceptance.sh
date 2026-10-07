#!/usr/bin/env bash
# Fresh native fixtures only; never stop or reuse an existing development fleet.
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/test-native-api.sh
bash scripts/test-go-deployment.sh
bash scripts/test-go-business.sh
bash scripts/test-native-matrix.sh
bash scripts/test-native-release.sh
