#!/usr/bin/env bash
#
# Обновление боевого Sayr. Дёргается из GitHub Actions по SSH:
#
#     ssh root@vps /usr/local/sbin/sayr-update
#
# Порядок: git fetch/reset → uv sync → дамп базы → alembic upgrade → restart →
# /healthz. Если healthz не ответил — откат кода на предыдущий коммит и повторная
# проверка; не помогло — выход с ненулевым кодом и хвостом журнала, чтобы это
# было видно прямо в логе Actions.
#
# ГДЕ ЛЕЖИТ РАБОЧАЯ КОПИЯ
# /usr/local/sbin/sayr-update, root:root, 755. Файл в репозитории — исходник.
# Разделение намеренное: скрипт не должен жить внутри каталога, который сам же
# перезаписывает git-пуллом. Скрипт предупредит, если копии разошлись.
#
# Коды выхода: 0 — ок, 2 — сервер настроен не так, 75 — деплой уже идёт,
# 1 — не поднялось.

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
PREDEPLOY_DUMP=${SAYR_PREDEPLOY_DUMP:-1}
# Кэш колёс после установки не нужен
CLEAN_UV_CACHE=${SAYR_CLEAN_UV_CACHE:-1}

LOCK=${SAYR_LOCK:-/var/lock/sayr-deploy.lock}

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

if [[ $PREDEPLOY_DUMP == 1 ]] && command -v pg_dump >/dev/null 2>&1; then
    # Один файл, перезаписывается каждый деплой: место фиксированное, но есть
    # точка возврата, если миграция испортит данные. Регулярные бэкапы —
    # отдельно, deploy/backup.sh в cron
    install -d -m 750 "$DUMP_DIR"
    log "дамп перед миграциями → $DUMP_DIR/pre-deploy.sql.gz"
    if sudo -n -u postgres pg_dump "$DB_NAME" | gzip >"$DUMP_DIR/pre-deploy.sql.gz.tmp"; then
        mv "$DUMP_DIR/pre-deploy.sql.gz.tmp" "$DUMP_DIR/pre-deploy.sql.gz"
    else
        rm -f "$DUMP_DIR/pre-deploy.sql.gz.tmp"
        warn "дамп не сделан; продолжаю без него"
    fi
fi

log "alembic upgrade head"
"$APP_DIR/.venv/bin/alembic" upgrade head

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
