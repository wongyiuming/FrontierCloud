#!/usr/bin/env bash
# Real sendfile/upload I/O and private Unix-counter acceptance; no host listeners.
set -euo pipefail
cd "$(dirname "$0")/.."
out="${FRONTIERCLOUD_TRAFFIC_EVIDENCE:?Set an absolute evidence directory}"
case "$out" in /*) ;; *) exit 1 ;; esac
mkdir -p "$out"
fixture="$(mktemp -d /tmp/fc-python-traffic.XXXXXXXX)"
name="fc-python-traffic-$(basename "$fixture")"
sampler=""
controller=""
cleanup() {
  touch "$fixture/finished"
  if [ -n "$sampler" ]; then wait "$sampler" || true; fi
  if [ -n "$controller" ]; then wait "$controller" || true; fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  case "$fixture" in /tmp/fc-python-traffic.*) rm -rf -- "$fixture" ;; *) exit 1 ;; esac
}
trap cleanup EXIT
mkdir "$fixture/counter" "$fixture/control"
chmod 0755 "$fixture" "$fixture/counter"
chmod 0777 "$fixture/control"
truncate -s 2097152 "$fixture/blob"
chmod 0644 "$fixture/blob"
cat > "$fixture/nginx.conf" <<'EOF'
load_module /usr/lib/nginx/modules/ngx_http_fc_traffic_module.so;
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 64; }
http {
  fc_business_traffic;
  access_log off;
  sendfile on;
  server {
    listen unix:/counter/counter.sock;
    location = /traffic { fc_business_traffic_status; }
  }
  server {
    listen 18081;
    client_max_body_size 3m;
    location = /traffic { fc_business_traffic_status; }
    location = /health { return 200 "ok"; }
    location = /blob { alias /fixture/blob; limit_rate 512k; }
    location = /held { alias /fixture/blob; limit_rate 1k; }
    location /internal/v1/storage/ {
      proxy_request_buffering off;
      proxy_pass http://127.0.0.1:18082;
    }
  }
}
EOF
docker run -d --name "$name" --label frontiercloud.acceptance=python-traffic --cpus 1 --memory 128m \
  --network none --mount "type=bind,source=$fixture,target=/fixture,readonly" \
  --mount "type=bind,source=$fixture/counter,target=/counter" \
  --entrypoint nginx "${FC_TRAFFIC_NGINX_IMAGE:-frontiercloud-acceptance-nginx}" \
  -c /fixture/nginx.conf -g 'daemon off;' > "$out/container.txt"
(
  while [ ! -e "$fixture/finished" ]; do
    docker stats --no-stream --format '{{json .}}' "$name" >> "$out/resources.jsonl" || break
    sleep 1
  done
) &
sampler=$!
(
  wait_marker() {
    for attempt in $(seq 1 800); do
      if [ -f "$fixture/finished" ]; then exit 0; fi
      if [ -f "$fixture/control/$1" ]; then return 0; fi
      sleep .1
    done
    return 1
  }
  wait_marker reload-request
  master="$(docker exec "$name" cat /tmp/nginx.pid)"
  case "$master" in ''|*[!0-9]*) exit 1 ;; esac
  children="$(docker exec "$name" cat "/proc/$master/task/$master/children")"
  read -r old_worker other <<< "$children"
  case "$old_worker" in ''|*[!0-9]*) exit 1 ;; esac
  test -z "${other:-}"
  docker exec "$name" nginx -s reload -c /fixture/nginx.conf
  touch "$fixture/control/reloaded"
  wait_marker kill-request
  docker exec "$name" sh -ec 'kill -9 "$1"' sh "$old_worker"
  touch "$fixture/control/killed"
) > "$out/controller.log" 2>&1 &
controller=$!
docker run --rm --label frontiercloud.acceptance=python-traffic --cpus 1 --memory 256m --user 10001:10001 \
  --network "container:$name" --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --mount "type=bind,source=$fixture/counter,target=/counter,readonly" \
  --mount "type=bind,source=$fixture/control,target=/control" \
  --workdir /workspace -e PYTHONDONTWRITEBYTECODE=1 \
  "${FC_TRAFFIC_PYTHON_IMAGE:-frontiercloud-python-unit-tools}" \
  timeout --kill-after=5s 80s python -m tests.business_traffic_native > "$out/native.log" 2>&1 || {
    cat "$out/native.log"; docker logs "$name"; exit 1;
  }
docker logs "$name" > "$out/nginx.log" 2>&1
wait "$controller"
cp "$out/native.log" "$out/native.json"
cat "$out/native.log"
