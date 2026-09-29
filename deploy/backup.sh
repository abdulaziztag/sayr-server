#!/usr/bin/env bash
#
# Ночной бэкап боевого Sayr. Запускает таймер sayr-backup.timer, руками — так:
#
#     systemctl start sayr-backup && journalctl -u sayr-backup -n 30 --no-pager
#
# Каждую ночь — дамп базы в $DEST/daily/db-ГГГГ-ММ-ДД.sql.gz, хранятся 14.
# Первый дамп недели остаётся ещё и в $DEST/weekly/db-ГГГГ-Wнн.sql.gz, рядом
# архив media той же недели; недель хранится 8. Если задан SAYR_BACKUP_REMOTE —
# daily/ и weekly/ зеркалятся за пределы сервера.
#
# Любая осечка — ненулевой код: служба становится failed, это видно
# в systemctl --failed и journalctl -u sayr-backup. Недоделанный бэкап,
# который молчит, хуже отсутствующего: о нём вспоминают в день, когда он нужен.
#
# Коды выхода: 0 — ок, 3 — здесь всё легло, но копия вне сервера не доехала,
# любой другой — дамп или архив не сделан.

set -Eeuo pipefail

APP_DIR=${SAYR_APP_DIR:-/root/Projects/sayr-server}
DB_NAME=${SAYR_DB_NAME:-sayr}
DEST=${SAYR_BACKUP_DIR:-/var/backups/sayr}
MEDIA_DIR=${SAYR_MEDIA_DIR:-$APP_DIR/media}
KEEP_DAILY=${SAYR_BACKUP_KEEP_DAILY:-14}
KEEP_WEEKLY=${SAYR_BACKUP_KEEP_WEEKLY:-8}

log()  { printf '==> %s\n' "$*"; }
warn() { printf ' /!\\ %s\n' "$*" >&2; }
die()  { printf ' ОШИБКА: %s\n' "$*" >&2; exit "${2:-1}"; }

on_err() {
    local code=$? line=${BASH_LINENO[0]}
    printf ' ОШИБКА на строке %s (код %s)\n' "$line" "$code" >&2
    exit "$code"
}
trap on_err ERR

# Недописанный файл не должен дожить до следующей ночи и попасть в копию
tmp=
trap '[[ -z $tmp ]] || rm -f -- "$tmp"' EXIT

# Одна строка из .env приложения. Не source целиком: там секреты, и shell
# не обязан понимать его синтаксис
env_value() {
    local line
    line=$(grep -E "^[[:space:]]*$1=" "$APP_DIR/.env" 2>/dev/null | tail -n 1) || return 0
    line=${line#*=}
    # Кавычки вокруг значения pydantic-settings снимает сам, rsync — нет
    line=${line%\"}; line=${line#\"}
    line=${line%\'}; line=${line#\'}
    printf '%s' "$line"
}

# Куда копировать вне сервера: окружение, иначе .env — там же, где остальные
# SAYR_. Поэтому в app/config.py есть поле backup_remote: незнакомый ключ
# в .env pydantic-settings не пропускает, и приложение не встало бы
REMOTE=${SAYR_BACKUP_REMOTE-$(env_value SAYR_BACKUP_REMOTE)}

# Ноль сотрёт и только что снятый дамп
[[ $KEEP_DAILY =~ ^[1-9][0-9]*$ ]] || die "SAYR_BACKUP_KEEP_DAILY=$KEEP_DAILY — нужно целое от 1"
[[ $KEEP_WEEKLY =~ ^[1-9][0-9]*$ ]] || die "SAYR_BACKUP_KEEP_WEEKLY=$KEEP_WEEKLY — нужно целое от 1"

# В базе телефоны и анкеты: файлы только для root
umask 077
install -d -m 750 "$DEST"
install -d -m 700 "$DEST/daily" "$DEST/weekly"

STAMP=$(date +%F)
# Неделя по ISO, и год к ней тоже ISO (%G, а не %Y): 29 декабря 2025 —
# это 2026-W01, а с %Y вышло бы 2025-W01, и ротация приняла бы свежий
# файл за годовалый
WEEK=$(date +%G-W%V)

# --- база -------------------------------------------------------------------

daily="$DEST/daily/db-$STAMP.sql.gz"
tmp="$daily.tmp"
log "дамп базы $DB_NAME → $daily"
# pipefail: упавший pg_dump роняет весь конвейер, а не прячется за gzip
sudo -n -u postgres pg_dump "$DB_NAME" | gzip >"$tmp" || die "pg_dump не отработал"

# Полный дамп pg_dump заканчивает этой строкой. Её нет — дамп пустой или
# оборван посреди (убили, кончилось место), и восстанавливать из него нечего.
# Хвост в десять строк, а не последняя: с PostgreSQL 16.10 и 17.6 за ней
# идёт ещё \unrestrict
[[ $(gzip -dc "$tmp" | tail -n 10) == *"PostgreSQL database dump complete"* ]] \
    || die "дамп пустой или оборван — в $daily не кладу"
mv "$tmp" "$daily"

weekly="$DEST/weekly/db-$WEEK.sql.gz"
if [[ ! -e $weekly ]]; then
    # Жёсткая ссылка вместо копии: места не занимает, а ротация daily/ файл
    # не заберёт — он живёт, пока жива хоть одна ссылка
    ln "$daily" "$weekly" 2>/dev/null || cp "$daily" "$weekly"
    log "первый дамп недели $WEEK → $weekly"
fi

# --- media ------------------------------------------------------------------

# Раз в неделю, а не каждую ночь: фото — самое тяжёлое, и меняются они редко.
# Отдельная проверка, а не внутри блока выше: не вышел архив — следующая ночь
# попробует снова, хотя дамп недели уже лежит
media="$DEST/weekly/media-$WEEK.tar.gz"
if [[ ! -e $media ]]; then
    [[ -d $MEDIA_DIR ]] || die "нет каталога $MEDIA_DIR — архивировать нечего"
    tmp="$media.tmp"
    log "архив media → $media"
    # Миниатюры не берём: пересобираются из photos (scripts/rebuild_thumbs.py)
    tar -czf "$tmp" --exclude="$(basename "$MEDIA_DIR")/thumbs" \
        -C "$(dirname "$MEDIA_DIR")" "$(basename "$MEDIA_DIR")"
    mv "$tmp" "$media"
fi

# --- ротация ----------------------------------------------------------------

# Оставляет последние $1 файлов из остальных аргументов. Имена датированы,
# а glob отдаёт их по алфавиту — значит, по времени; на mtime не опираемся,
# его сбивает любое копирование
prune() {
    local keep=$1
    shift
    while (($# > keep)); do
        rm -f -- "$1"
        shift
    done
}

shopt -s nullglob
prune "$KEEP_DAILY" "$DEST"/daily/db-*.sql.gz
prune "$KEEP_WEEKLY" "$DEST"/weekly/db-*.sql.gz
prune "$KEEP_WEEKLY" "$DEST"/weekly/media-*.tar.gz
shopt -u nullglob

# --- копия вне сервера ------------------------------------------------------

if [[ -z $REMOTE ]]; then
    warn "копия вне сервера не настроена (SAYR_BACKUP_REMOTE пуст): бэкапы лежат" \
         "на том же диске, что и база, и умрут вместе с ним"
    log "готово: $daily"
    exit 0
fi

# Зеркало, а не складирование: на той стороне ровно то же, что здесь, и место
# там не растёт без предела. Трогаются только daily/ и weekly/ внутри адреса.
# Ошибка копии — отдельный код: локальный бэкап при этом цел
case $REMOTE in
    rclone:*)
        # rclone:<remote>:<путь> — всё, что после первого «rclone:», уходит
        # в rclone как есть
        target=${REMOTE#rclone:}
        log "rclone sync → $target"
        rclone sync "$DEST/daily" "$target/daily" \
            && rclone sync "$DEST/weekly" "$target/weekly" \
            || die "rclone sync в $target не прошёл — локальный бэкап цел" 3
        ;;
    *)
        # user@host:/путь или локальный каталог (примонтированный диск).
        # BatchMode: таймер ночью не ответит на вопрос о пароле, и служба
        # висела бы до утра
        target=${REMOTE%/}
        log "rsync → $target/"
        rsync -a --delete -e "ssh -o BatchMode=yes -o ConnectTimeout=30" \
            "$DEST/daily" "$DEST/weekly" "$target/" \
            || die "rsync в $target не прошёл — локальный бэкап цел" 3
        ;;
esac

log "готово: $daily, копия в $REMOTE"
