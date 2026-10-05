#!/usr/bin/env bash
# GitHub-only lightweight checks. Full acceptance runs on the development host.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/check_ci_budget.py
python3 scripts/check_cpu_boundary.py
python3 scripts/check_release_policy.py
python3 scripts/check_english_comments.py
python3 -m unittest tests.test_ci_budget tests.test_repository_policy tests.test_schema_bootstrap
git diff --check
for file in static/js/*.js; do node --check "$file"; done
node tests/admin_ui_smoke.mjs
node tests/player_cache_smoke.mjs
node tests/network_observation_smoke.mjs
node tests/audio_continuous_stream_smoke.mjs
