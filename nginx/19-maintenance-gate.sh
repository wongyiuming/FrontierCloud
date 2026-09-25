#!/bin/sh
set -eu

template=/etc/nginx/templates/nginx.conf.template
marker='include /etc/nginx/maintenance-gate.conf;'
if ! grep -Fq "$marker" "$template"; then
    sed -i '/client_max_body_size 64k;/a\        include /etc/nginx/maintenance-gate.conf;' "$template"
fi
