#!/bin/sh
set -eu

environment=$(printf '%s' "${ENVIRONMENT:-production}" | tr '[:upper:]' '[:lower:]')

case "$environment" in
  development|test) nginx_mode=development ;;
  production) nginx_mode=production ;;
  *)
    echo "ENVIRONMENT must be development, test, or production for nginx" >&2
    exit 1
    ;;
esac

source_dir="/etc/nginx/environments/$nginx_mode"
template_dir=/etc/nginx/templates/runtime

install -d -m 0755 "$template_dir"
for config_name in environment-servers.conf public-listen.conf public-tls.conf; do
  install -m 0644 "$source_dir/$config_name" "$template_dir/$config_name.template"
done

echo "nginx: selected $nginx_mode configuration for ENVIRONMENT=$environment"
