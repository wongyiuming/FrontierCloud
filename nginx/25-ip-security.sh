#!/bin/sh
set -eu

snapshot=/app/data/.ip-security/active-bans.tsv
rules=/etc/nginx/runtime/ip-security.conf
candidate=/etc/nginx/runtime/ip-security.candidate
previous=/etc/nginx/runtime/ip-security.previous

render_rules() {
    awk -v now="$(date +%s)" -f /etc/nginx/render-security.awk "$snapshot" > "$candidate"
}

install -d -m 0755 /etc/nginx/runtime
# Web readiness guarantees the initial snapshot. Never bootstrap silently empty.
render_rules
mv "$candidate" "$rules"
nginx -t

watch_rules() {
    while [ ! -s /var/run/nginx.pid ]; do sleep 1; done
    master_pid=$(cat /var/run/nginx.pid)
    while kill -0 "$master_pid" 2>/dev/null; do
        if render_rules; then
            if ! cmp -s "$candidate" "$rules"; then
                cp "$rules" "$previous"
                mv "$candidate" "$rules"
                if nginx -t && nginx -s reload; then
                    echo 'nginx: applied IP security rules'
                else
                    mv "$previous" "$rules"
                    echo 'nginx: rejected IP security update; retaining previous rules' >&2
                fi
            fi
        else
            echo 'nginx: invalid/missing IP security snapshot; retaining previous rules' >&2
        fi
        sleep 1
    done
}

# The container PID namespace stops this helper when its Nginx master exits.
# Only local files are polled; unchanged policies cause no database calls/reload.
watch_rules &
