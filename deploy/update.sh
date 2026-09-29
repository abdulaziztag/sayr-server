#!/usr/bin/env bash
#
# Обновление боевого Sayr. Дёргается из GitHub Actions по SSH:
#
#     ssh root@vps /usr/local/sbin/sayr-update
#
# Порядок: git fetch/reset → uv sync → дамп базы → alembic upgrade → restart →
# /healthz. Упало что-то до рестарта — каталог и .venv возвращаются на прежний
# коммит, служба так и работает на нём. Если healthz не ответил — откат кода
# на предыдущий коммит и повторная проверка; не помогло — выход с ненулевым
# кодом и хвостом журнала, чтобы это было видно прямо в логе Actions.
#
# ГДЕ ЛЕЖИТ РАБОЧАЯ КОПИЯ
# /usr/local/sbin/sayr-update, root:root, 755. Файл в репозитории — исходник.
# Разделение намеренное: скрипт не должен жить внутри каталога, который сам же
# перезаписывает git-пуллом. Скрипт предупредит, если копии разошлись.
#
# Коды выхода: 0 — ок, 2 — сервер настроен не так, 75 — деплой уже идёт,
# 130 — прервали, 1 — не поднялось.

set -Eeuo pipefail

APP_DIR=${SAYR_APP_DIR:-/root/Projects/sayr-server}
SERVICE=${SAYR_SERVICE:-sayr}
BRANCH=${SAYR_BRANCH:-main}
REMOTE=${SAYR_REMOTE:-origin}
UV=${SAYR_UV:-/usr/local/bin/uv}

HEALTH_URL=${SAYR_HEALTH_URL:-http://127.0.0.1:8000/healthz}
HEALTH_TRIES=${SAYR_HEALTH_TRIES:-20}
HEALTH_DELAY=${SAYR_HEALTH_DELAY:-2}

DB_NAME=${SAYR_DB_NAME:-sayr}
DUMP_DIR=${SAYR_DUMP_DIR:-/var/backups/sayr}
# Без дампа миграции не катятся. Осознанно без него — SAYR_PREDEPLOY_DUMP=0
PREDEPLOY_DUMP=${SAYR_PREDEPLOY_DUMP:-1}
# Сколько последних дампов перед деплоем держать
PREDEPLOY_KEEP=${SAYR_PREDEPLOY_KEEP:-10}
# Кэш колёс после установки не нужен
CLEAN_UV_CACHE=${SAYR_CLEAN_UV_CACHE:-1}

LOCK=${SAYR_LOCK:-/var/lock/sayr-deploy.lock}
# Куда пишут git и uv, когда возвращают каталог: терминала, из которого
# запускали, к этому моменту может уже не быть
RESTORE_LOG=${SAYR_RESTORE_LOG:-/var/log/sayr-update.log}

# Приватный репозиторий: fetch идёт по ssh с отдельным deploy-ключом.
# Ключ вне APP_DIR — внутри его снёс бы git clean
KEY=${SAYR_KEY:-/etc/sayr/deploy_key}
KNOWN_HOSTS=${SAYR_KNOWN_HOSTS:-/etc/sayr/known_hosts}

log()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m /!\\ %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m ОШИБКА: %s\033[0m\n' "$*" >&2; exit "${2:-1}"; }

on_err() {
    local code=$? line=${BASH_LINENO[0]}
    printf '\033[31m ОШИБКА на строке %s (код %s)\033[0m\n' "$line" "$code" >&2
    journalctl -u "$SERVICE" -n 40 --no-pager >&2 || true
    exit "$code"
}
trap on_err ERR

# Коммит, на который вернуть каталог, если выходим до рестарта. Пока он
# задан, служба крутится на старом коде, а в каталоге уже новый: брось его
# так после упавшего sync или миграции — и первый же рестарт (ручной,
# Restart=on-failure, перезагрузка сервера) поднял бы новый код на старой
# схеме. Возврат висит на EXIT, поэтому срабатывает и на die, и на ERR.
# git и uv без смены каталога: RESTORE_TO задаётся уже после cd "$APP_DIR"
RESTORE_TO=
restore_code() {
    [[ -n $RESTORE_TO ]] || return 0
    local to=$RESTORE_TO out=$RESTORE_LOG
    RESTORE_TO=
    # Сюда попадают и после обрыва ssh, когда stdout и stderr уже мертвы:
    # любая запись в них — ошибка (EPIPE, EIO пропавшего терминала). Под
    # set -e первая же такая ошибка — хоть в warn — оборвала бы возврат
    # до git reset. Поэтому дальше без set -e и ERR, вывод git и uv —
    # в файл, а сообщения — как получится
    set +e
    trap - ERR
    warn "возвращаю каталог и .venv на $to — служба не перезапускалась и работает на нём"
    : 2>/dev/null >>"$out" || out=/dev/null
    {
        printf '== %s: возврат на %s\n' "$(date '+%F %T')" "$to"
        git reset -q --hard "$to" && "$UV" sync --frozen --no-dev
    } >>"$out" 2>&1 \
        || warn "вернуть $to не вышло (вывод — в $out) — каталог в промежуточном состоянии, до рестарта чинить руками"
}
trap restore_code EXIT
# Оборванный ssh или Ctrl-C — тоже выход через EXIT, а не смерть посреди
# sync. С терминалом обрыв — это HUP. Без терминала (так зовёт Actions)
# сигнала нет, зато первая же запись в закрытый канал дала бы SIGPIPE,
# и bash умер бы на месте, не вернув каталог. С игнором запись просто
# падает с EPIPE — обычная ошибка команды, её ловят set -e и ERR
trap 'exit 130' INT TERM HUP
trap '' PIPE

# --- проверки ---------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "запускать от root" 2

# Один деплой за раз. В workflow есть concurrency, но защита нужна и от ручного
# запуска параллельно с автоматическим
exec 9>"$LOCK"
flock -n 9 || die "деплой уже идёт, этот прогон пропущен" 75

[[ -d $APP_DIR/.git ]] || die "$APP_DIR — не git-репозиторий" 2
[[ -x $UV ]] || die "нет uv по пути $UV" 2
[[ -f $APP_DIR/.env ]] || die "нет $APP_DIR/.env — приложение без него не стартует" 2
[[ -x $APP_DIR/.venv/bin/alembic ]] || die "нет $APP_DIR/.venv — сделай первый uv sync вручную" 2
[[ -f $KEY ]] || die "нет deploy-ключа $KEY — репозиторий приватный" 2
[[ -f $KNOWN_HOSTS ]] || die "нет $KNOWN_HOSTS — не с чем сверить отпечаток github.com" 2
# Ноль стёр бы и дамп, снятый только что
[[ $PREDEPLOY_KEEP =~ ^[1-9][0-9]*$ ]] || die "SAYR_PREDEPLOY_KEEP=$PREDEPLOY_KEEP — нужно целое от 1" 2

export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o BatchMode=yes"

cd "$APP_DIR"

url=$(git remote get-url "$REMOTE")
[[ $url == git@github.com:* || $url == ssh://git@github.com/* ]] || die \
    "remote $REMOTE = $url; для приватного репозитория нужен ssh:
     git -C $APP_DIR remote set-url $REMOTE git@github.com:abdulaziztag/sayr-server.git" 2

# --- код --------------------------------------------------------------------

PREV=$(git rev-parse HEAD)
log "текущий коммит $PREV"

log "git fetch $REMOTE/$BRANCH"
git fetch --prune "$REMOTE" "$BRANCH"

# reset, а не pull: результат не зависит от локальных правок и переживает
# force-push. Untracked-файлы (.env, media/) reset не трогает
RESTORE_TO=$PREV
git reset --hard "$REMOTE/$BRANCH"
NEW=$(git rev-parse HEAD)
log "новый коммит   $NEW"

if ! cmp -s "$APP_DIR/deploy/update.sh" /usr/local/sbin/sayr-update; then
    warn "deploy/update.sh в репозитории разошёлся с /usr/local/sbin/sayr-update."
    warn "Сейчас работает старая копия. Обновить:"
    warn "  install -m 755 -o root -g root $APP_DIR/deploy/update.sh /usr/local/sbin/sayr-update"
fi

log "uv sync --frozen --no-dev"
"$UV" sync --frozen --no-dev
[[ $CLEAN_UV_CACHE == 1 ]] && "$UV" cache clean >/dev/null

# --- база -------------------------------------------------------------------

if [[ $PREDEPLOY_DUMP == 0 ]]; then
    warn "SAYR_PREDEPLOY_DUMP=0 — миграции без дампа перед ними"
else
    # Точка возврата, если миграция испортит данные. Файл на каждый деплой:
    # один перезаписываемый терял бы её уже на следующей выкатке — ровно
    # тогда, когда порчу замечают. Регулярные бэкапы — отдельно,
    # deploy/backup.sh по таймеру sayr-backup.timer
    install -d -m 750 "$DUMP_DIR"
    dump="$DUMP_DIR/pre-deploy-$(date +%Y%m%d-%H%M%S).sql.gz"
    log "дамп перед миграциями → $dump"
    # Без дампа не мигрируем: warn и дальше значил бы миграцию без пути назад,
    # а увидел бы его только тот, кто дочитал лог Actions. Строкой «dump
    # complete» pg_dump заканчивает только полный дамп (см. deploy/backup.sh)
    if ! sudo -n -u postgres pg_dump "$DB_NAME" | gzip >"$dump.tmp" \
        || [[ $(gzip -dc "$dump.tmp" | tail -n 10) != *"PostgreSQL database dump complete"* ]]; then
        rm -f "$dump.tmp"
        die "дамп перед миграциями не сделан — без него не мигрирую. Осознанно без дампа: SAYR_PREDEPLOY_DUMP=0" 1
    fi
    mv "$dump.tmp" "$dump"

    # Имена с датой, glob отдаёт их по алфавиту — значит, по времени
    shopt -s nullglob
    dumps=("$DUMP_DIR"/pre-deploy-*.sql.gz)
    shopt -u nullglob
    for ((i = 0; i < ${#dumps[@]} - PREDEPLOY_KEEP; i++)); do
        rm -f -- "${dumps[i]}"
    done
fi

log "alembic upgrade head"
"$APP_DIR/.venv/bin/alembic" upgrade head
# Схема уже новая, дальше рестарт — за откат с этого места отвечает
# проверка healthz ниже
RESTORE_TO=

# --- перезапуск и проверка --------------------------------------------------

wait_healthy() {
    local i
    for ((i = 1; i <= HEALTH_TRIES; i++)); do
        if curl -fsS --max-time 3 -o /dev/null "$HEALTH_URL"; then
            log "healthz ответил (попытка $i)"
            return 0
        fi
        sleep "$HEALTH_DELAY"
    done
    return 1
}

log "systemctl restart $SERVICE"
systemctl restart "$SERVICE"
# Служба Sayr Admin держит свой вход в Telegram и тоже живёт на новом коде.
# Не установлена — пропускаем: без неё комнаты живут без групп
if systemctl list-unit-files sayr-tg.service --no-legend 2>/dev/null | grep -q sayr-tg; then
    log "systemctl restart sayr-tg"
    systemctl restart sayr-tg || warn "sayr-tg не перезапустилась — смотрите journalctl -u sayr-tg"
fi

if wait_healthy; then
    log "готово: $SERVICE работает на коммите $NEW"
    exit 0
fi

# --- откат ------------------------------------------------------------------

warn "healthz молчит $((HEALTH_TRIES * HEALTH_DELAY)) с — откатываю код на $PREV"
journalctl -u "$SERVICE" -n 40 --no-pager >&2 || true

[[ $PREV == "$NEW" ]] && die "коммит не менялся, откатывать нечего — чинить руками" 1

# Откатываем ТОЛЬКО код. alembic downgrade автоматом не делаем сознательно:
# миграции обычно аддитивные, а downgrade умеет удалять колонки вместе с
# данными — потерять каталог хуже, чем полежать
trap - ERR
set +e

git reset --hard "$PREV"
"$UV" sync --frozen --no-dev
systemctl restart "$SERVICE"

if wait_healthy; then
    die "деплой $NEW не поднялся, откатились на $PREV — сервис жив, но код старый" 1
fi

journalctl -u "$SERVICE" -n 60 --no-pager >&2
die "сервис не поднялся ни на $NEW, ни на $PREV — нужен ручной разбор" 1
