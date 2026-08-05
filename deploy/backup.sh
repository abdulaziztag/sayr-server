#!/usr/bin/env bash
# Бэкап базы и загруженных файлов. От root в cron:
#   0 4 * * * /opt/sayr/deploy/backup.sh
set -euo pipefail

DEST=${1:-/var/backups/sayr}
KEEP_DAYS=${KEEP_DAYS:-14}
STAMP=$(date +%F)

mkdir -p "$DEST"

sudo -u postgres pg_dump sayr | gzip >"$DEST/db-$STAMP.sql.gz"
tar -czf "$DEST/media-$STAMP.tar.gz" -C /opt/sayr media

# Место на диске ограничено — старое подчищаем
find "$DEST" -name '*.gz' -mtime +"$KEEP_DAYS" -delete

echo "готово: $DEST/db-$STAMP.sql.gz, $DEST/media-$STAMP.tar.gz"
