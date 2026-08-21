#!/bin/sh
set -eu

umask 077
mkdir -p /backups /textfile
: "${MYSQL_BACKUP_USER:?MYSQL_BACKUP_USER is required}"

write_metrics() {
  success="$1"
  timestamp="$2"
  size="$3"
  temporary=/textfile/backup.prom.tmp
  printf '%s\n' \
    '# HELP frontiercloud_backup_last_run_success Whether the most recent MySQL backup succeeded.' \
    '# TYPE frontiercloud_backup_last_run_success gauge' \
    "frontiercloud_backup_last_run_success ${success}" \
    '# HELP frontiercloud_backup_last_success_timestamp_seconds Unix timestamp of the most recent successful MySQL backup.' \
    '# TYPE frontiercloud_backup_last_success_timestamp_seconds gauge' \
    "frontiercloud_backup_last_success_timestamp_seconds ${timestamp}" \
    '# HELP frontiercloud_backup_last_size_bytes Size of the most recent successful MySQL backup.' \
    '# TYPE frontiercloud_backup_last_size_bytes gauge' \
    "frontiercloud_backup_last_size_bytes ${size}" > "$temporary"
  chmod 0644 "$temporary"
  mv "$temporary" /textfile/backup.prom
}

last_success=0
while true; do
  now="$(date +%s)"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  temporary="/backups/.frontiercloud-${stamp}.sql.tmp"
  destination="/backups/frontiercloud-${stamp}.sql"

  if mysqldump \
      --host=mysql \
      --user="$MYSQL_BACKUP_USER" \
      --single-transaction \
      --quick \
      --routines \
      --events \
      --triggers \
      --all-databases > "$temporary"; then
    mv "$temporary" "$destination"
    last_success="$now"
    size="$(wc -c < "$destination" | tr -d ' ')"
    write_metrics 1 "$last_success" "$size"
    echo "MySQL backup completed at ${stamp}: ${size} bytes"
  else
    rm -f "$temporary"
    write_metrics 0 "$last_success" 0
    echo "MySQL backup failed at ${stamp}" >&2
  fi

  find /backups -type f -name 'frontiercloud-*.sql' -mtime +7 -delete
  sleep 86400
done
