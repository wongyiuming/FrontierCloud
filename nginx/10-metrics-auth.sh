#!/bin/sh
set -eu

case "${METRICS_BASIC_USER:-}${METRICS_BASIC_PASSWORD:-}" in
  *"
"*) echo "metrics credentials must not contain newlines" >&2; exit 1 ;;
esac

if [ -z "${METRICS_BASIC_USER:-}" ] || [ -z "${METRICS_BASIC_PASSWORD:-}" ]; then
  echo "metrics credentials are required" >&2
  exit 1
fi

install -d -o root -g nginx -m 0750 /run/frontiercloud
htpasswd -bcB /run/frontiercloud/metrics.htpasswd "$METRICS_BASIC_USER" "$METRICS_BASIC_PASSWORD" >/dev/null
chown root:nginx /run/frontiercloud/metrics.htpasswd
chmod 0640 /run/frontiercloud/metrics.htpasswd

# The log exporter starts as soon as Nginx is running. Create its source before
# the Nginx master starts so the exporter never misses the file during startup.
install -d -m 0755 /var/log/nginx
touch /var/log/nginx/access_log.log
chmod 0640 /var/log/nginx/access_log.log
