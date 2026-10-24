#!/usr/bin/env bash
# Product Compose with test-only CA/bridge overrides. Never binds a host port.
set -euo pipefail
cd "$(dirname "$0")/.."
runtime="$(mktemp -d /tmp/fc-python-certificate-storage.XXXXXXXX)"
project="$(basename "$runtime" | tr '[:upper:].' '[:lower:]-')"
cleanup() {
  docker compose --env-file "$runtime/test.env" -p "$project" -f docker-compose.storage.yaml -f "$runtime/test.compose.yaml" down -v --remove-orphans >/dev/null 2>&1 || true
  case "$runtime" in /tmp/fc-python-certificate-storage.*) rm -rf -- "$runtime" ;; *) exit 1 ;; esac
}
trap cleanup EXIT
mkdir -p "$runtime/ca" "$runtime/data"
chmod 0700 "$runtime/ca"
docker build -t frontiercloud-python-storage:test-tls -f Dockerfile .
docker run --rm --network none \
  --mount "type=bind,source=$runtime/ca,target=/test-ca" --workdir /app --user 0 \
  frontiercloud-python-storage:test-tls python -c '
from pathlib import Path
from tests.certificate_fixtures import TestCA
from cryptography.hazmat.primitives import serialization
ca=TestCA()
Path("/test-ca/root.crt").write_bytes(ca.root.public_bytes(serialization.Encoding.PEM))
Path("/test-ca/private.pem").write_bytes(ca.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))'
cat > "$runtime/Dockerfile.test-trust" <<'EOF'
FROM frontiercloud-python-storage:test-tls
USER 0
COPY root.crt /usr/local/share/ca-certificates/frontiercloud-test-only-root.crt
RUN update-ca-certificates
USER 10001:10001
EOF
docker build -t frontiercloud-python-storage:test-tls-trust -f "$runtime/Dockerfile.test-trust" "$runtime/ca"
cat > "$runtime/test.compose.yaml" <<EOF
services:
  secrets-init: {image: frontiercloud-python-storage:test-tls-trust}
  media-init: {image: frontiercloud-python-storage:test-tls-trust}
  certificate-init: {image: frontiercloud-python-storage:test-tls-trust}
  certificate-agent:
    image: frontiercloud-python-storage:test-tls-trust
    network_mode: !reset null
    command: [python, -m, tests.storage_certificate_agent_fixture]
    volumes: ["$runtime/ca:/test-ca:ro"]
  web:
    image: frontiercloud-python-storage:test-tls-trust
    mem_limit: 512m
    ports: !reset []
    networks:
      default:
        aliases: [node.4399.com]
  stun:
    ports: !reset []
networks:
  default: {internal: true}
EOF
compose() { docker compose --env-file "$runtime/test.env" -p "$project" -f docker-compose.storage.yaml -f "$runtime/test.compose.yaml" "$@"; }
evidence="${FRONTIERCLOUD_CERTIFICATE_EVIDENCE:-/tmp/frontiercloud-python-certificate-evidence}"
mkdir -p "$evidence"
for profile in direct relay; do
  port=443
  endpoint=https://node.4399.com
  [ "$profile" = direct ] || { port=8443; endpoint=https://node.4399.com:8443; }
  cat > "$runtime/test.env" <<EOF
TLS_ENABLED=true
SERVER_NAME=node.4399.com
STORAGE_TRANSPORT=$profile
STORAGE_PORT=$port
STORAGE_ENDPOINT=$endpoint
DATA_DIRECTORY=$runtime/data
EOF
  if ! compose up -d --no-build --wait --wait-timeout 150; then
    compose logs --tail 50
    exit 1
  fi
  # Issuance readiness can precede its next 30-second public serving observation.
  for _ in $(seq 1 80); do
    if compose exec -T web python -m app.services.certmanager.probe --https-ready >/dev/null 2>&1; then break; fi
    sleep 1
  done
  compose exec -T web python -m tests.certificate_storage_container > "$evidence/storage-$profile-real.json"
  python3 - "$(compose ps -q certificate-agent)" "$(compose ps -q web)" "$evidence/storage-$profile-limits.json" <<'PY'
import json,pathlib,subprocess,sys
values={}
for role,identifier in zip(('certificate-agent','web'),sys.argv[1:3]):
    actual=json.loads(subprocess.check_output(['docker','inspect',identifier]))[0]['HostConfig']
    values[role]={name:actual[name] for name in ('NanoCpus','Memory','PidsLimit','ReadonlyRootfs')}
pathlib.Path(sys.argv[3]).write_text(json.dumps(values,sort_keys=True))
assert values['certificate-agent']['NanoCpus']==100000000 and values['certificate-agent']['Memory']==128*1024**2
assert values['web']['NanoCpus']==1000000000 and values['web']['Memory']==512*1024**2
PY
  compose exec -T web python -m tests.certificate_status_container > "$evidence/storage-$profile-status-concurrent.json"
  # One Binding request per storage profile, serialized with the TLS checks.
  compose exec -T web python -c 'import os,socket,struct;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(3);t=os.urandom(12);s.sendto(struct.pack("!HHI",1,0,0x2112a442)+t,("stun",3478));r,_=s.recvfrom(2048);assert r[:2]==b"\x01\x01" and r[8:20]==t;s.close();print("Storage UDP STUN verified")'
  compose exec -T certificate-agent python -c 'import socket;s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(("0.0.0.0",80));s.listen();s.close();print("HTTP-01 listener released")'
  compose restart certificate-agent web
  for _ in $(seq 1 80); do
    if compose exec -T web python -m app.services.health_probe; then break; fi
    sleep 1
  done
  compose exec -T web python -m app.services.health_probe
  # ASGI health can precede the restarted controller's socket and serving
  # observation. Wait for verified certificate readiness, not merely Web health.
  for _ in $(seq 1 80); do
    if compose exec -T web python -m app.services.certmanager.probe --https-ready >/dev/null 2>&1; then break; fi
    sleep 1
  done
  compose exec -T web python -m app.services.certmanager.probe --https-ready
  compose exec -T web python -m tests.certificate_status_container > "$evidence/storage-$profile-status-after-restart.json"
  compose down -v --remove-orphans
  # Guarded, fixture-only data. Each profile tests a fresh identity.
  case "$runtime/data" in /tmp/fc-python-certificate-storage.*/data) rm -rf -- "$runtime/data"; mkdir -p "$runtime/data" ;; *) exit 1 ;; esac
done
