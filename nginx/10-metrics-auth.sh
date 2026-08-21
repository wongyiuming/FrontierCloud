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

install -d -m 0700 /run/frontiercloud
htpasswd -bcB /run/frontiercloud/metrics.htpasswd "$METRICS_BASIC_USER" "$METRICS_BASIC_PASSWORD" >/dev/null
chmod 0600 /run/frontiercloud/metrics.htpasswd
