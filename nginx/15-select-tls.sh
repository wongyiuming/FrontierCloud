#!/bin/sh
set -eu

tls_enabled=$(printf '%s' "${TLS_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')
public_bind_address=${PUBLIC_BIND_ADDRESS:-0.0.0.0}

case "$tls_enabled" in
  true|1|yes|on) nginx_mode=https ;;
  false|0|no|off) nginx_mode=http ;;
  *)
    echo "TLS_ENABLED must be a boolean for nginx" >&2
    exit 1
    ;;
esac

case "$public_bind_address" in
  0.0.0.0) ipv6_enabled=false ;;
  ::) ipv6_enabled=true ;;
  *)
    echo "PUBLIC_BIND_ADDRESS must be 0.0.0.0 or :: for nginx" >&2
    exit 1
    ;;
esac

source_dir="/etc/nginx/transport/$nginx_mode"
template_dir=/etc/nginx/templates/runtime

install -d -m 0755 "$template_dir"
for config_name in extra-servers.conf public-listen.conf public-tls.conf; do
  install -m 0644 "$source_dir/$config_name" "$template_dir/$config_name.template"
  if [ "$ipv6_enabled" = true ]; then
    sed -i 's/^\([[:space:]]*\)# IPV6 /\1/' "$template_dir/$config_name.template"
  fi
done

echo "nginx: selected $nginx_mode configuration with PUBLIC_BIND_ADDRESS=$public_bind_address"
