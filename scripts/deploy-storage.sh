#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose -f docker-compose.storage.yaml config --quiet
docker compose -f docker-compose.storage.yaml build web
# Validate the profile before Compose reserves any host listener. `run` publishes
# no service ports and only imports configuration; it does not start an issuer.
docker compose -f docker-compose.storage.yaml run --rm --no-deps web python -c \
  'from app.core.config import settings; print("Storage network/TLS configuration verified")'
if ! docker compose -f docker-compose.storage.yaml up -d --no-build --wait --wait-timeout 600; then
  docker compose -f docker-compose.storage.yaml exec -T certificate-agent python -c \
    'import asyncio,json; from app.services.certmanager import certificate_control; print(json.dumps(asyncio.run(certificate_control()), ensure_ascii=False))' || true
  printf '%s\n' 'Storage HTTPS is not ready. Check automatic certificate status, DNS and public TCP 80. The controller keeps retrying; no pairing package has been generated.' >&2
  exit 1
fi
ready=false
for _ in $(seq 1 90); do
  if docker compose -f docker-compose.storage.yaml exec -T web python -m app.services.certmanager.probe --https-ready >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 2
done
if [ "$ready" != true ]; then
  printf '%s\n' 'Storage process is running, but public HTTPS serving proof is not ready. Pairing was not generated; inspect the automatic TLS status.' >&2
  exit 1
fi
printf '%s\n' 'Verified storage HTTPS is ready. Import this one-use package on the Master within five minutes.'
docker compose -f docker-compose.storage.yaml exec -T web python -m app.services.certmanager.pair < /dev/null
