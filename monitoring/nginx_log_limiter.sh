#!/bin/sh
set -eu

case "${NGINX_METRIC_LOG_MAX_BYTES:-}" in
  ''|*[!0-9]*) echo "NGINX_METRIC_LOG_MAX_BYTES must be a positive integer" >&2; exit 1 ;;
esac
if [ "$NGINX_METRIC_LOG_MAX_BYTES" -le 0 ]; then
  echo "NGINX_METRIC_LOG_MAX_BYTES must be positive" >&2
  exit 1
fi

log=/mnt/nginxlogs/access_log.log
while true; do
  if [ -f "$log" ]; then
    size="$(wc -c < "$log" | tr -d ' ')"
    if [ "$size" -ge "$NGINX_METRIC_LOG_MAX_BYTES" ]; then
      : > "$log"
      echo "Nginx metrics log truncated after reaching ${size} bytes"
    fi
  fi
  sleep 60
done
