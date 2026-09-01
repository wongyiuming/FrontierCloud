#!/bin/sh
set -eu

install -d -o nginx -g nginx -m 0755 /var/log/nginx
touch /var/log/nginx/access_log.log
chown nginx:nginx /var/log/nginx/access_log.log
chmod 0644 /var/log/nginx/access_log.log
