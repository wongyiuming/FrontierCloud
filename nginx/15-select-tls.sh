#!/bin/sh
set -eu

tls_enabled=$(printf '%s' "${TLS_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')

case "$tls_enabled" in
  true|1|yes|on) nginx_mode=production ;;
  false|0|no|off) nginx_mode=development ;;
  *)
    echo "TLS_ENABLED must be a boolean for nginx" >&2
    exit 1
    ;;
esac

source_dir="/etc/nginx/environments/$nginx_mode"
template_dir=/etc/nginx/templates/runtime

install -d -m 0755 "$template_dir"
for config_name in environment-servers.conf public-listen.conf public-tls.conf; do
  install -m 0644 "$source_dir/$config_name" "$template_dir/$config_name.template"
done

echo "nginx: selected $nginx_mode configuration for TLS_ENABLED=$tls_enabled"
