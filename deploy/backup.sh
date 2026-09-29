#!/usr/bin/env bash
#
# Ночной бэкап боевого Sayr. Запускает таймер sayr-backup.timer, руками — так:
#
#     systemctl start sayr-backup && journalctl -u sayr-backup -n 30 --no-pager
#
# Каждую ночь — дамп базы в $DEST/daily/db-ГГГГ-ММ-ДД.sql.gz, хранятся 14.
# Первый дамп недели остаётся ещё и в $DEST/weekly/db-ГГГГ-Wнн.sql.gz, рядом
# архив media той же недели; недель хранится 8. Если задан SAYR_BACKUP_REMOTE —
# daily/ и weekly/ копируются за пределы сервера.
#
# Любая осечка — ненулевой код: служба становится failed, это видно
# в systemctl --failed и journalctl -u sayr-backup. Недоделанный бэкап,
# который молчит, хуже отсутствующего: о нём вспоминают в день, когда он нужен.
# Не вышел архив media — дамп базы всё равно проходит ротацию и уезжает
# с сервера, а ненулевой код ставится в самом конце.
#
# Коды выхода: 0 — ок, 3 — здесь всё легло, но копия вне сервера не доехала,
# любой другой — дамп или архив не сделан.

set -Eeuo pipefail

APP_DIR=${SAYR_APP_DIR:-/root/Projects/sayr-server}
DB_NAME=${SAYR_DB_NAME:-sayr}
DEST=${SAYR_BACKUP_DIR:-/var/backups/sayr}
KEEP_DAILY=${SAYR_BACKUP_KEEP_DAILY:-14}
KEEP_WEEKLY=${SAYR_BACKUP_KEEP_WEEKLY:-8}
# Сколько места должно остаться после архива media. Диск общий с базой
# и чужими сайтами: бэкап, который его забил, положил бы и то и другое
MIN_FREE_MB=${SAYR_BACKUP_MIN_FREE_MB:-1024}

log()  { printf '==> %s\n' "$*"; }
warn() { printf ' /!\\ %s\n' "$*" >&2; }
die()  { printf ' ОШИБКА: %s\n' "$1" >&2; exit "${2:-1}"; }

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
    # || true внутри подстановки: нет такой строки — не ошибка, и ERR,
    # который подстановка наследует от set -E, не должен о ней кричать
    line=$(grep -E "^[[:space:]]*$1=" "$APP_DIR/.env" 2>/dev/null | tail -n 1 || true)
    [[ -n $line ]] || return 0
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

# media — там же, где его ищет приложение: окружение, .env, иначе
# media/ рядом с кодом. Относительный путь приложение считает от своего
# каталога (WorkingDirectory), так же и здесь
MEDIA_DIR=${SAYR_MEDIA_DIR:-$(env_value SAYR_MEDIA_DIR)}
MEDIA_DIR=${MEDIA_DIR:-$APP_DIR/media}
[[ $MEDIA_DIR == /* ]] || MEDIA_DIR=$APP_DIR/$MEDIA_DIR

# Ноль сотрёт и только что снятый дамп
[[ $KEEP_DAILY =~ ^[1-9][0-9]*$ ]] || die "SAYR_BACKUP_KEEP_DAILY=$KEEP_DAILY — нужно целое от 1"
[[ $KEEP_WEEKLY =~ ^[1-9][0-9]*$ ]] || die "SAYR_BACKUP_KEEP_WEEKLY=$KEEP_WEEKLY — нужно целое от 1"
[[ $MIN_FREE_MB =~ ^[0-9]+$ ]] || die "SAYR_BACKUP_MIN_FREE_MB=$MIN_FREE_MB — нужно целое"

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
# попробует снова, хотя дамп недели уже лежит.
#
# Осечка здесь не обрывает прогон: свежий дамп базы важнее, и он обязан
# пройти ротацию и уехать с сервера. Ошибка запоминается, код — в конце
media_error=
media="$DEST/weekly/media-$WEEK.tar.gz"
if [[ -e $media ]]; then
    : # архив этой недели уже есть
elif [[ ! -d $MEDIA_DIR ]]; then
    media_error="нет каталога $MEDIA_DIR — архив media не сделан"
else
    # Фото почти не сжимаются: архиву нужно примерно столько, сколько весит
    # media (с миниатюрами — с запасом), и после него должен остаться запас
    need_kb=$(($(du -sk "$MEDIA_DIR" | cut -f1) + MIN_FREE_MB * 1024))
    free_kb=$(df -Pk "$DEST/weekly" | awk 'NR == 2 { print $4 }')
    if ((free_kb < need_kb)); then
        media_error="свободно $((free_kb / 1024)) МБ, архиву media с запасом нужно $((need_kb / 1024)) — не делаю"
    else
        tmp="$media.tmp"
        log "архив media → $media"
        # Миниатюры не берём: пересобираются из photos (scripts/rebuild_thumbs.py).
        # Код 1 у GNU tar — «файл менялся, пока читали»: на архив пришлась
        # загрузка через /report или админку. Архив цел, только это не
        # мгновенный снимок — ронять из-за этого ночь незачем
        rc=0
        tar -czf "$tmp" --exclude="$(basename "$MEDIA_DIR")/thumbs" \
            -C "$(dirname "$MEDIA_DIR")" "$(basename "$MEDIA_DIR")" || rc=$?
        if ((rc > 1)); then
            rm -f -- "$tmp"
            media_error="tar не отработал (код $rc) — архив media не сделан"
        else
            ((rc == 0)) || warn "файлы в media менялись, пока шёл архив: он цел, но это не мгновенный снимок"
            mv "$tmp" "$media"
        fi
    fi
fi
[[ -z $media_error ]] || warn "$media_error; дамп базы цел, ротация и копия идут дальше"

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

# Полон ли набор: в каталоге $1 не меньше $2 файлов по каждому шаблону
# из остальных аргументов, то есть ротация уже отрезает лишнее
full_set() {
    local dir=$1 keep=$2 pattern files
    shift 2
    for pattern; do
        shopt -s nullglob
        files=("$dir"/$pattern)
        shopt -u nullglob
        ((${#files[@]} >= keep)) || return 1
    done
}

# Копирует $DEST/$1 в $target/$1. Там стирается то, чего здесь уже нет, —
# та же ротация, и место на той стороне не растёт без предела. Но только
# при полном наборе здесь: копия нужна именно тогда, когда здесь пусто —
# сервер пересобран и таймер включили раньше, чем вернули /var/backups/sayr,
# или каталог стёрли по ошибке. Зеркало в эту ночь стёрло бы и там всё,
# кроме одного свежего дампа. Неполный набор — только докопировать новое
offsite() {
    local sub=$1 rsync_delete= rclone_verb=copy
    shift
    if full_set "$DEST/$sub" "$@"; then
        rsync_delete=--delete rclone_verb=sync
    else
        log "$sub/: здесь неполный набор — старое на той стороне не трогаю"
    fi
    case $REMOTE in
        rclone:*)
            # Таймауты у rclone свои по умолчанию: на соединение и на тишину
            rclone "$rclone_verb" "$DEST/$sub" "$target/$sub"
            ;;
        *)
            # BatchMode: таймер ночью не ответит на вопрос о пароле. --timeout
            # и ServerAlive — на связь, умершую без разрыва: без них rsync
            # ждал бы её вечно, служба висела бы activating, и следующие
            # ночи таймер пропускал бы молча
            rsync -a $rsync_delete --timeout=600 \
                -e "ssh -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=30 -o ServerAliveCountMax=4" \
                "$DEST/$sub" "$target/"
            ;;
    esac
}

remote_error=
if [[ -z $REMOTE ]]; then
    warn "копия вне сервера не настроена (SAYR_BACKUP_REMOTE пуст): бэкапы лежат" \
         "на том же диске, что и база, и умрут вместе с ним"
else
    case $REMOTE in
        # rclone:<remote>:<путь> — всё, что после первого «rclone:», уходит
        # в rclone как есть
        rclone:*) target=${REMOTE#rclone:} ;;
        # user@host:/путь или локальный каталог (примонтированный диск)
        *) target=${REMOTE%/} ;;
    esac
    # Трогаются только daily/ и weekly/ внутри адреса. Ошибка копии —
    # отдельный код: локальный бэкап при этом цел
    log "копия вне сервера → $target"
    offsite daily "$KEEP_DAILY" 'db-*.sql.gz' \
        && offsite weekly "$KEEP_WEEKLY" 'db-*.sql.gz' 'media-*.tar.gz' \
        || remote_error="копия в $target не прошла — локальный бэкап цел"
fi

[[ -z $media_error ]] || die "$media_error${remote_error:+; $remote_error}"
[[ -z $remote_error ]] || die "$remote_error" 3
log "готово: $daily${REMOTE:+, копия в $REMOTE}"
