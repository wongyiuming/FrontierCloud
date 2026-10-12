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
docker build -t frontiercloud-acceptance-web .
docker build -t frontiercloud-acceptance-nginx -f nginx/Dockerfile .
tools_context="$(mktemp -d /tmp/fc-python-unit-tools.XXXXXXXX)"
cleanup() {
  # Fixtures own their containers and volumes. This removes only our tools context.
  case "$tools_context" in /tmp/fc-python-unit-tools.*) rm -rf -- "$tools_context" ;; *) exit 1 ;; esac
}
trap cleanup EXIT
cat > "$tools_context/Dockerfile" <<'EOF'
FROM frontiercloud-acceptance-web
USER 0
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs && rm -rf /var/lib/apt/lists/*
EOF
docker build -t frontiercloud-python-unit-tools "$tools_context"
docker run --rm --cpus 1 --network none --user 0 \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --tmpfs /workspace/data:size=2g,mode=1777 --tmpfs /tmp:size=2g,mode=1777 \
  --workdir /workspace frontiercloud-python-unit-tools \
  python -m unittest discover -s tests -p 'test_*.py' > "$evidence/unit.log" 2>&1
tail -8 "$evidence/unit.log"
python3 scripts/check_cpu_boundary.py
python3 scripts/validate_env_contract.py
python3 scripts/check_release_policy.py
python3 scripts/check_english_comments.py
find static/js -name '*.js' -print0 | xargs -0 -n1 node --check
node tests/admin_ui_smoke.mjs
node tests/player_cache_smoke.mjs
node tests/network_observation_smoke.mjs
bash scripts/test-python-certificate-nginx.sh > "$evidence/certificate-nginx.log" 2>&1
bash scripts/test-python-certificate-storage.sh > "$evidence/certificate-storage.log" 2>&1
# The fleet checks each single-core Web's CPU recovery through check_cpu_quiescence.py.
"$browser_python" -m tests.federation_stack --output "$evidence/federation.json" > "$evidence/federation.log" 2>&1
bash scripts/test-python-operator.sh > "$evidence/operator.log" 2>&1
test -z "$(git status --porcelain --untracked-files=all)"
test "$(git rev-parse HEAD)" = "$source_sha"
printf '%s\n' "$source_sha" > "$evidence/source-commit.txt"
printf '%s\n' "Complete native acceptance passed for $source_sha"
