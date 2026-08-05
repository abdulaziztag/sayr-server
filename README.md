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

Два варианта. TLS обязателен в обоих: без него cookie админки и логин идут
открытым текстом, а Android с боевого домена заблокирует cleartext.

### Вариант A — без Docker (мало места на диске)

Занимает порядка 200 МБ: окружение ~80 МБ, код с фотографиями ~20 МБ,
Python от uv ~50 МБ, Postgres из apt ~50 МБ. Вариант с Docker при тех же
задачах съедает около полутора гигабайт — сам движок плюс образы Postgres
и Python. Команды под Debian/Ubuntu, на другом дистрибутиве поменяются
названия пакетов.

```bash
# 1. Postgres из системных пакетов
sudo apt update && sudo apt install -y postgresql
sudo -u postgres psql -c "CREATE USER sayr WITH PASSWORD 'СИЛЬНЫЙ_ПАРОЛЬ';"
sudo -u postgres psql -c "CREATE DATABASE sayr OWNER sayr;"

# 2. uv — один бинарник, ставим системно
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh

# 3. Отдельный пользователь и код
sudo useradd --system --home-dir /opt/sayr --shell /usr/sbin/nologin sayr
sudo mkdir -p /opt/sayr && sudo chown sayr:sayr /opt/sayr
sudo -u sayr git clone https://github.com/abdulaziztag/sayr-server.git /opt/sayr
cd /opt/sayr

# 4. Окружение. В Debian 12 системный Python — 3.11, нужен 3.12+, его ставит uv
sudo -u sayr uv python install 3.12
sudo -u sayr uv sync --frozen --no-dev
sudo -u sayr uv cache clean          # кэш колёс больше не нужен, освобождает место

# 5. Секреты
sudo -u sayr cp .env.example .env
sudo -u sayr nano .env               # пароль админки, SAYR_SECRET_KEY, строка к БД
sudo chmod 600 .env

# 6. Схема и стартовый каталог
sudo -u sayr .venv/bin/alembic upgrade head
sudo -u sayr .venv/bin/python -m seed.seed

# 7. Служба
sudo cp deploy/sayr.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sayr
systemctl status sayr
```

Postgres из apt по умолчанию слушает только лоопбек — так и оставь, наружу
он не нужен. Приложение тоже слушает `127.0.0.1:8000`, снаружи его показывает
reverse proxy.

Обновление потом:

```bash
cd /opt/sayr
sudo -u sayr git pull
sudo -u sayr uv sync --frozen --no-dev
sudo -u sayr .venv/bin/alembic upgrade head
sudo systemctl restart sayr
```

### Вариант B — Docker

```bash
git clone https://github.com/abdulaziztag/sayr-server.git sayr-server && cd sayr-server
cp .env.example .env && $EDITOR .env      # пароли, ключ, POSTGRES_PASSWORD
docker compose -f compose.prod.yml up -d --build
docker compose -f compose.prod.yml exec app python -m seed.seed
```

`compose.prod.yml` применяет миграции при старте сам. Postgres наружу не
пробрасывается: `docker publish` обходит ufw, поэтому открытый 5432 фаервол
бы не закрыл.

### Reverse proxy и TLS

Пример конфига — `deploy/Caddyfile` (Caddy сам получает и продлевает сертификат;
статику из `media` отдаёт с диска, минуя приложение). Подойдёт и nginx с certbot,
проксировать на `127.0.0.1:8000`.

### Что не забыть

- **`media/`** — фото и GPX, залитые через админку, живут только там. В git их
  нет. Без Docker это каталог `/opt/sayr/media`, с Docker — том `media`;
  без тома они пропадут при пересоздании контейнера.
- **Бэкап** — `deploy/backup.sh` в cron: дамп БД плюс архив `media`.
- **Здоровье** — `GET /healthz` ходит в БД, годится для мониторинга.
- **Сид идемпотентен** — повторный запуск обновит поля мест и не продублирует
  фото. Реальные фотографии есть у 15 мест в `seed/data/photos/`, остальным
  генерируются заглушки.
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
