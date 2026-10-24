#!/usr/bin/env bash
# Reproducible compatible-provider, service and actual browser acceptance.
set -euo pipefail
cd "$(dirname "$0")/.."
evidence="${FC_S3_EVIDENCE_DIR:?Set an absolute, new S3 evidence directory}"
browser_python="${FC_BROWSER_PYTHON:-python3}"
case "$evidence" in /*) ;; *) echo 'S3 evidence path must be absolute' >&2; exit 1 ;; esac
test ! -e "$evidence"
umask 077
mkdir -p "$evidence/private"
private="$evidence/private"
fixture="$private/s3-compatible-fixture"
browser_root="$private/s3-browser-fixture-native"
export S3_FIXTURE="$fixture"
export S3_BROWSER_ROOT="$browser_root"
export S3_BROWSER_LIFECYCLE=1
export S3_BROWSER_PLAYBACK=1
export S3_BROWSER_READ_EXPIRY=1
export S3_BROWSER_DATABASE="${FC_S3_BROWSER_DATABASE:-sqlite}"
export S3_BROWSER_IDLE="${FC_S3_LONG_IDLE:-0}"
export PYTHONDONTWRITEBYTECODE=1
"$browser_python" -m tests.s3_compatible_fixture --directory "$fixture" > "$evidence/fixture.log" 2>&1
project="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$fixture/manifest.json")"
compose=(docker compose -p "$project" -f "$fixture/compose.json")
cleanup() {
  result=$?
  if test -f "$browser_root/admin/compose.json"; then
    browser_project="$(python3 -c 'from pathlib import Path; import hashlib,sys; print("fc-acceptance-"+hashlib.sha256(str(Path(sys.argv[1]).resolve()).encode()).hexdigest()[:12]+"-admin")' "$browser_root")"
    docker compose -p "$browser_project" -f "$browser_root/admin/compose.json" stop --timeout 10 web > "$private/web-stop.log" 2>&1 || true
    docker compose -p "$browser_project" -f "$browser_root/admin/compose.json" stop --timeout 10 > "$private/browser-stop.log" 2>&1 || true
  fi
  "${compose[@]}" stop --timeout 15 > "$private/provider-stop.log" 2>&1 || true
  exit "$result"
}
trap cleanup EXIT
"${compose[@]}" up -d > "$private/provider-start.log" 2>&1
native() {
  docker run --rm --cpus 1 --memory 512m --user 0 --network "$project" \
    --mount "type=bind,source=$fixture,target=/fixture" \
    --mount "type=bind,source=$PWD,target=/workspace,readonly" --workdir /workspace \
    --tmpfs /workspace/data:size=128m,mode=1777 --tmpfs /tmp:size=256m,mode=1777 \
    -e PYTHONDONTWRITEBYTECODE=1 -e DB_TYPE=sqlite -e S3_FIXTURE=/fixture \
    -e "S3_TRANSACTION_ROOT=/fixture/${4:-transactions-native}" \
    -e "S3_FIXTURE_PHASE=${2:-initial}" "$project:client" \
    timeout --kill-after=5s "${3:-120}s" python -m "tests.$1"
}
native s3_service_native initial > "$evidence/provider-initial.json" 2> "$evidence/provider-initial.log"
"${compose[@]}" restart s3 > "$private/provider-restart.log" 2>&1
native s3_service_native restart > "$evidence/provider-restart.json" 2> "$evidence/provider-restart-client.log"
native s3_uploads_native > "$evidence/transactions.json" 2> "$evidence/transactions.log"
native s3_credential_revocation_native prepare 120 transactions-credential-revocation > "$evidence/credential-revocation-prepare.json" 2> "$evidence/credential-revocation-prepare.log"
"$browser_python" -m tests.s3_credential_revocation_fixture --directory "$fixture" > "$evidence/credential-revocation-provider.json" 2> "$private/credential-revocation-provider.log"
native s3_credential_revocation_native resume 420 transactions-credential-revocation > "$evidence/credential-revocation.json" 2> "$evidence/credential-revocation.log"
browser_timeout=1000
if test "$S3_BROWSER_IDLE" = 1; then browser_timeout=2850; fi
timeout --kill-after=30s "${browser_timeout}s" "$browser_python" -m tests.s3_browser_native > "$evidence/browser.log" 2>&1
install -m 0600 "$browser_root/result.json" "$evidence/browser.json"
python3 - "$evidence" <<'PY'
import json,pathlib,sys
root=pathlib.Path(sys.argv[1])
for name in ('provider-initial.json','provider-restart.json','transactions.json','credential-revocation-prepare.json',
             'credential-revocation-provider.json','credential-revocation.json','browser.json'):
    raw=(root/name).read_text()
    if 'PRIVATE KEY-----' in raw or 'X-Amz-Signature=' in raw:
        raise RuntimeError('unsafe_s3_evidence')
    json.loads(raw)
revocation=json.loads((root/'credential-revocation.json').read_text())
assert revocation['passed'] and not revocation['tls_errors_ignored'] and not revocation['source_clock_or_sql_deadline_changed']
result=json.loads((root/'browser.json').read_text())
assert result['passed'] and not result['tls_errors_ignored'] and not result['aws_real_bucket']
print(json.dumps(dict(passed=True,browser_checks=len(result['checks']),cpu_samples=result['cpu_samples'])))
PY
