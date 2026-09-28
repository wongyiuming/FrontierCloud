#!/bin/sh
set -eu

root=/root/frontiercloud-dev
candidate=/root/frontiercloud-audit-20260927/source
result=/root/frontiercloud-audit-20260927/candidate-ten-final-20260928
nodes='master direct-1 direct-2 direct-3 relay-1 relay-2 relay-3 relay-4 relay-5 relay-6'

[ "$(id -u)" = 0 ]
[ -f "$root/inventory.json" ]
[ ! -e "$result" ]
install -m 600 /dev/null "$root/cd-paused"
systemctl stop frontiercloud-dev-cd.service || true

compose() {
    docker exec "fc-dev-host-$1" docker compose --project-directory /node/repo -p frontiercloud \
        -f /node/repo/docker-compose.yaml -f /node/override.json "$2" -d --no-build --wait --wait-timeout 240
}

restore() {
    for node in $nodes; do
        compose "$node" up || true
    done
    rm -f "$root/cd-paused"
    systemctl start frontiercloud-dev-cd.timer || true
}
trap restore EXIT INT TERM

for node in $nodes; do
    docker exec "fc-dev-host-$node" docker compose --project-directory /node/repo -p frontiercloud \
        -f /node/repo/docker-compose.yaml -f /node/override.json stop
done

cd "$candidate"
docker build -t frontiercloud-acceptance-web .
docker build -t frontiercloud-acceptance-nginx -f nginx/Dockerfile .
docker build -t frontiercloud-updater -f updater/Dockerfile .
/root/frontiercloud-audit-20260927/venv/bin/python -m tests.federation_ten_node \
    --directory "$result"
