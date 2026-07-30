#!/bin/bash
# Локальная PostgreSQL+PostGIS через Homebrew — запасной путь, когда Docker недоступен.
# Идемпотентен: поднимает сервер, создаёт роль sayr и БД sayr / sayr_test с PostGIS.
set -euo pipefail

PG_FORMULA=$(ls /opt/homebrew/opt 2>/dev/null | grep -E '^postgresql@[0-9]+$' | sort -V | tail -1)
if [ -z "$PG_FORMULA" ]; then
  echo "PostgreSQL не найден. Установите: brew install postgis" >&2
  exit 1
fi

PGBIN="/opt/homebrew/opt/$PG_FORMULA/bin"
PGDATA="/opt/homebrew/var/$PG_FORMULA"
export PATH="$PGBIN:$PATH"

if ! "$PGBIN/pg_isready" -q 2>/dev/null; then
  echo "Стартую $PG_FORMULA (PGDATA=$PGDATA)…"
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/server.log" start
  for i in $(seq 1 20); do "$PGBIN/pg_isready" -q && break; sleep 1; done
fi
"$PGBIN/pg_isready" || { echo "PostgreSQL не поднялся, лог: $PGDATA/server.log" >&2; exit 1; }

psql postgres -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sayr') THEN
    CREATE ROLE sayr LOGIN PASSWORD 'sayr' CREATEDB;
  END IF;
END $$;
SQL

for DB in sayr sayr_test; do
  if ! psql -lqt | cut -d'|' -f1 | grep -qw "$DB"; then
    createdb -O sayr "$DB"
  fi
  # PostGIS опционален: near-фильтр работает на встроенном SQL-хаверсине
  psql "$DB" -c "CREATE EXTENSION IF NOT EXISTS postgis;" 2>/dev/null \
    || echo "(PostGIS для $DB недоступен — ок, приложению не требуется)"
done

echo "OK: роль sayr, БД sayr и sayr_test готовы (порт 5432)."
