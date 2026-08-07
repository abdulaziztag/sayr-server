#!/usr/bin/env bash
# Бэкап базы и загруженных файлов. От root в cron:
#   0 4 * * * /opt/sayr/deploy/backup.sh
set -euo pipefail

DEST=${1:-/var/backups/sayr}
KEEP_DAYS=${KEEP_DAYS:-14}
STAMP=$(date +%F)

mkdir -p "$DEST"

# Через tmp+mv: упавший посреди pg_dump иначе оставляет обрезанный архив,
# который при восстановлении выглядит как нормальный бэкап
sudo -u postgres pg_dump sayr | gzip >"$DEST/db-$STAMP.sql.gz.tmp"
mv "$DEST/db-$STAMP.sql.gz.tmp" "$DEST/db-$STAMP.sql.gz"
tar -czf "$DEST/media-$STAMP.tar.gz.tmp" -C "${MEDIA_ROOT:-/root/Projects/sayr-server}" media
mv "$DEST/media-$STAMP.tar.gz.tmp" "$DEST/media-$STAMP.tar.gz"

# Место на диске ограничено — старое подчищаем
find "$DEST" -name '*.gz' -mtime +"$KEEP_DAYS" -delete

echo "готово: $DEST/db-$STAMP.sql.gz, $DEST/media-$STAMP.tar.gz"
