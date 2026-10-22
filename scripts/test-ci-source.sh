#!/usr/bin/env bash
# Hosted checks are bounded; complete acceptance runs on the development host.
set -euo pipefail
cd "$(dirname "$0")/.."
python scripts/check_ci_budget.py
python -m compileall -q app main.py scripts
python scripts/check_cpu_boundary.py
python scripts/check_release_policy.py
python scripts/validate_env_contract.py
python scripts/check_english_comments.py
python -m unittest -v \
  tests.test_repository_policy \
  tests.test_deployment_contract \
  tests.test_federation_contract \
  tests.test_operator_release \
  tests.test_operator_source \
  tests.test_native_acceptance_cleanup
git diff --check
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
