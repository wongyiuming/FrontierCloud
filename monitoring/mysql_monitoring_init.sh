#!/bin/sh
set -eu

validate_identifier() {
  name="$1"
  value="$2"
  case "$value" in
    ''|*[!A-Za-z0-9_]*)
      echo "$name must contain only letters, digits, and underscores" >&2
      exit 1
      ;;
  esac
}

validate_secret() {
  name="$1"
  value="$2"
  if [ "${#value}" -lt 16 ]; then
    echo "$name must contain at least 16 characters" >&2
    exit 1
  fi
  case "$value" in
    *[!A-Za-z0-9_.,:@%+=-]*)
      echo "$name contains unsupported characters" >&2
      exit 1
      ;;
  esac
}

: "${MYSQL_DATABASE:?MYSQL_DATABASE is required}"
: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}"
: "${MYSQL_EXPORTER_USER:?MYSQL_EXPORTER_USER is required}"
: "${MYSQL_EXPORTER_PASSWORD:?MYSQL_EXPORTER_PASSWORD is required}"
: "${MYSQL_BACKUP_USER:?MYSQL_BACKUP_USER is required}"
: "${MYSQL_BACKUP_PASSWORD:?MYSQL_BACKUP_PASSWORD is required}"

validate_identifier MYSQL_DATABASE "$MYSQL_DATABASE"
validate_identifier MYSQL_EXPORTER_USER "$MYSQL_EXPORTER_USER"
validate_identifier MYSQL_BACKUP_USER "$MYSQL_BACKUP_USER"
validate_secret MYSQL_ROOT_PASSWORD "$MYSQL_ROOT_PASSWORD"
validate_secret MYSQL_EXPORTER_PASSWORD "$MYSQL_EXPORTER_PASSWORD"
validate_secret MYSQL_BACKUP_PASSWORD "$MYSQL_BACKUP_PASSWORD"

export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
mysql --protocol=SOCKET --socket=/var/run/mysqld/mysqld.sock --user=root --batch <<SQL
CREATE USER IF NOT EXISTS '${MYSQL_EXPORTER_USER}'@'%' IDENTIFIED BY '${MYSQL_EXPORTER_PASSWORD}';
ALTER USER '${MYSQL_EXPORTER_USER}'@'%' IDENTIFIED BY '${MYSQL_EXPORTER_PASSWORD}';
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO '${MYSQL_EXPORTER_USER}'@'%';

CREATE USER IF NOT EXISTS '${MYSQL_BACKUP_USER}'@'%' IDENTIFIED BY '${MYSQL_BACKUP_PASSWORD}';
ALTER USER '${MYSQL_BACKUP_USER}'@'%' IDENTIFIED BY '${MYSQL_BACKUP_PASSWORD}';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, PROCESS ON *.* TO '${MYSQL_BACKUP_USER}'@'%';
FLUSH PRIVILEGES;
SQL
unset MYSQL_PWD

echo "MySQL monitoring and backup users are ready"
