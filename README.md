# Sayr API

Бэкенд гида по природным местам Узбекистана: каталог мест с фильтрами, погода,
GPX-треки, отметки «пойду в этот день». Клиенты — нативные iOS и Android.

FastAPI · SQLAlchemy 2 (async) · Alembic · PostgreSQL · SQLAdmin · uv

## Что где

| Путь                  | Что                                                        |
|-----------------------|------------------------------------------------------------|
| `app/api/`            | Ручки: места, регионы, намерения («кто ещё идёт»)          |
| `app/services/`       | Прокси погоды (Open-Meteo), миниатюры                       |
| `app/admin.py`        | Админка на `/admin` — кураторское наполнение каталога       |
| `alembic/versions/`   | Миграции                                                    |
| `seed/`               | Стартовый каталог: `data/places.json` + фото в `data/photos`|

## Переменные окружения

Скопируй `.env.example` в `.env` и заполни. **`SAYR_ADMIN_PASSWORD` и
`SAYR_SECRET_KEY` обязательны — без них приложение не стартует.** Так задумано:
`secret_key` подписывает cookie сессии админа, и со значением по умолчанию из
публичного репозитория в `/admin` заходят подделанной cookie, минуя форму входа.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # SAYR_SECRET_KEY
```

## Деплой на VPS

Нужны docker и docker compose. TLS обязателен: без него cookie админки и логин
идут открытым текстом, а Android с боевого домена заблокирует cleartext.

```bash
git clone <repo> sayr-server && cd sayr-server
cp .env.example .env && $EDITOR .env      # пароли, ключ, POSTGRES_PASSWORD
docker compose -f compose.prod.yml up -d --build
```

`compose.prod.yml` сам применяет миграции при старте. Первый раз залей каталог:

```bash
docker compose -f compose.prod.yml exec app python -m seed.seed
```

Сид идемпотентен — повторный запуск обновит поля мест и не продублирует фото.
Фотографии 15 мест лежат в `seed/data/photos/`, остальным генерируются заглушки.

Дальше — reverse proxy на `127.0.0.1:8000`. Пример для Caddy:

```
sayr.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Наружу порт публикует только proxy: Postgres в compose вообще не пробрасывается,
приложение слушает лоопбек. `docker publish` обходит ufw, поэтому пробрасывать
5432 наружу нельзя даже с фаерволом.

### Что не забыть

- **Том `media`** — фото и GPX, залитые через админку, живут только там.
  В git их нет; без тома они пропадут при пересоздании контейнера.
- **Бэкап** — `pg_dump` по cron плюс копия тома `media`.
- **Здоровье** — `GET /healthz` ходит в БД, годится для мониторинга.
- **Адрес в клиентах** зашит в сборку: после деплоя поменять
  `APIClient.baseURL` (iOS) и `API_BASE_URL` (Android) на боевой домен.

## Локальная разработка

```bash
cp .env.example .env          # SAYR_ADMIN_COOKIE_SECURE=false для http
docker compose up -d          # PostgreSQL на 127.0.0.1:5432
uv sync
uv run alembic upgrade head
uv run python -m seed.seed
uv run uvicorn app.main:app --reload
```

Без docker база поднимается через Homebrew: `bash scripts/local_db.sh`.

- API-доки: http://localhost:8000/docs
- Админка: http://localhost:8000/admin

```bash
uv run pytest
```

Тестам нужна отдельная база `sayr_test` — её создаёт `docker/initdb/01-test-db.sh`
при первом подъёме контейнера.

## Известные ограничения

- `POST/DELETE /api/v1/places/{slug}/intents` доверяют `device_id` от клиента:
  аккаунтов нет. Накрутить счётчик «кто ещё идёт» можно скриптом — при росте
  трафика понадобится rate-limit по IP.
- Кэш погоды живёт в памяти процесса: при нескольких воркерах каждый греет свой.
