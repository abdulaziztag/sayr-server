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

`deploy/nginx-sayr.conf` проксирует на `127.0.0.1:8000` и режет частоту
запросов к входу и формам (ответ 429) и к телеметрии (ответ 503: 4xx
приложения считают окончательным отказом и стёрли бы данные). Кладётся в
`/etc/nginx/sites-available/sayr`, симлинк в `sites-enabled`, потом
`nginx -t && systemctl reload nginx` и `certbot --nginx -d <домен>`.
Именно `reload`, а не `restart`: на общем сервере restart уронил бы
соседние сайты.

Конфига для Caddy больше нет: на бою nginx, а заготовка отдавала бы
`/media` с диска целиком — вместе с `reports/` и `deleted-photos/`.

Домена нет? Бесплатный вариант — [duckdns.org](https://www.duckdns.org):
заводится имя, указывается IP сервера, Let's Encrypt выдаёт на него
сертификат как на обычный домен.

### Боевой стенд

Сейчас развёрнуто на `https://sayr.duckdns.org` — Ubuntu 24.04, код
в `/root/Projects/sayr-server`, служба `sayr` в systemd, nginx + certbot,
деплой по push в main (см. `.github/workflows/ci.yml`).

### Что не забыть

- **`media/`** — фото и GPX, залитые через админку, живут только там. В git их
  нет. Без Docker это каталог `/opt/sayr/media`, с Docker — том `media`;
  без тома они пропадут при пересоздании контейнера.
- **`media/reports/`** — файлы, приложенные к заявкам с формы `/report`.
  Лежат на том же томе, но наружу не отдаются: под `/media` смонтированы
  `photos`, `thumbs` и `gpx` поимённо, а не весь каталог. Владельцу они
  открываются из админки (`/admin/report-file/{id}`), по сессии.
- **Бэкап** — `deploy/backup.sh` по таймеру `sayr-backup.timer`, см. ниже.
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

- API-доки: http://localhost:8000/docs — только с `SAYR_API_DOCS=1` в `.env`;
  на бою они выключены, схема раскрывала бы и ручки за флагами
- Админка: http://localhost:8000/admin. Вход живёт 12 часов и кончается
  со сменой пароля. После пяти промахов подряд вход с адреса запирается
  на минуту, дальше вдвое дольше, до 15 минут; перезапуск службы снимает паузу

```bash
uv run pytest
```

Тестам нужна отдельная база `sayr_test` — её создаёт `docker/initdb/01-test-db.sh`
при первом подъёме контейнера.

## Планы по дням и тяжёлые треки

У многодневок план пишет человек (спека
`docs/superpowers/specs/2026-09-13-multiday-plan-design.md`): часы клуба,
дни, станции. Планы лежат в `seed/data/plans.json` и грузятся отдельно
от основного сида, идемпотентно по паре «место, название»:

```bash
uv run python -m seed.load_plans                     # все места из файла
uv run python -m seed.load_plans --only adelunga-peak
```

В файле у места могут быть и `tracks` — они заливаются тем же путём,
что треки из `places.json`. Форма в админке («Планы по дням») —
запасной путь.

На боевом стенде отдельного пользователя нет: скрипты запускаются
от root из корня репозитория, где лежит `.env` со строкой к базе:

```bash
cd /root/Projects/sayr-server && .venv/bin/python -m seed.load_plans
```

GPX длиннее 2000 точек прореживаются при загрузке в админку и в сиде
(`services/gpx.thin_if_heavy`). Файлы, залитые раньше, чистит разовый
скрипт — сначала посмотреть, потом `--apply` после копии `media/gpx`:

```bash
uv run python -m seed.thin_tracks
uv run python -m seed.thin_tracks --apply
```

На боевом стенде — так же от root из `/root/Projects/sayr-server`,
через `.venv/bin/python`.

## Известные ограничения

- `POST/DELETE /api/v1/places/{slug}/intents` доверяют `device_id` от клиента:
  аккаунтов нет. Накрутить счётчик «кто ещё идёт» можно скриптом — при росте
  трафика понадобится rate-limit по IP.
- Кэш погоды живёт в памяти процесса: при нескольких воркерах каждый греет свой.

## Пуши по расписанию

Раздел «Уведомления» в админке: заголовок, текст, время по Ташкенту,
необязательный slug места. Раз в минуту `sayr-push.timer` запускает
`python -m app.push.send_due`: созревшие объявления уходят всем живым
токенам из `push_tokens` — iPhone напрямую в APNs, Android через FCM.
Спека: `docs/superpowers/specs/2026-09-05-push-announcements-design.md`.

Ключи — в `.env` (см. `.env.example`): `SAYR_APNS_KEY_PATH` + `SAYR_APNS_KEY_ID`
для Apple, `SAYR_FCM_SERVICE_ACCOUNT_PATH` для Firebase. Платформа без ключей
не роняет прогон: её устройства попадают в «не дошло», причина — в `last_error`.
Погашенные токены (Apple или Google ответили, что установки больше нет)
стираются через 30 дней тем же тиком — `/privacy` это обещает.

Таймер ставится один раз, руками:

```bash
cp deploy/sayr-push.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now sayr-push.timer
journalctl -u sayr-push.service -n 20     # что ушло на последнем тике
```

## Вход через Telegram и Apple

`POST /api/v1/auth/telegram` (ID-токен из библиотеки Telegram или код с PKCE),
`POST /api/v1/auth/apple`, привязка `POST /api/v1/me/telegram`. Спека:
`docs/superpowers/specs/2026-09-30-telegram-apple-login-design.md`.

Настройки — в `.env` (см. `.env.example`): `SAYR_TG_LOGIN_BOT_ID` (пусто —
вход через Telegram выключен), `SAYR_TG_LOGIN_CLIENT_SECRET` (только для обмена
кода), `SAYR_APPLE_KEY_ID` + `SAYR_APPLE_KEY_PATH` (ключ Sign in with Apple
для отзыва доступа при удалении аккаунта; без него вход с Apple работает,
а отзывать нечем). «Войти по номеру» в приложениях — `SAYR_PHONE_LOGIN`.

Refresh-токен Apple лежит в базе зашифрованным ключом из `SAYR_SECRET_KEY`:
сменили ключ — старые токены не расшифруются, и отзывать их станет нечем.
Отзыв, не прошедший сразу после удаления, повторяется раз в час (цикл ротации
в `app/stats.py`) и через 30 дней бросается с ошибкой в журнале.

## Бэкапы

Каждую ночь в 03:30 по Ташкенту `sayr-backup.timer` запускает
`deploy/backup.sh`: дамп базы в `/var/backups/sayr/daily` (14 последних),
первый дамп недели и архив `media` без миниатюр — в
`/var/backups/sayr/weekly` (8 последних недель). Пустой или оборванный
дамп, упавший `pg_dump`, не вышедший архив, зависание дольше трёх часов —
служба failed, это видно в `systemctl --failed`.

Место: 14 + 8 дампов базы, 10 дампов перед деплоями и 8 архивов `media`
почти в полный размер (фото не сжимаются) — то есть примерно восемь
`media`. Если после архива на диске осталось бы меньше
`SAYR_BACKUP_MIN_FREE_MB` (1024), архив недели не делается и служба
failed: диск общий с базой и чужими сайтами.

Всё это лежит на том же диске, что и база. Копия вне сервера включается
строкой в `.env`: `SAYR_BACKUP_REMOTE=user@host:/путь` (rsync по ssh
с ключом root, без пароля) или `SAYR_BACKUP_REMOTE=rclone:<remote>:<путь>`.
На той стороне в `daily/` и `weekly/` та же ротация, что здесь, но
старое там стирается, только когда здесь полный набор (14 ночей
и 8 недель). После переустановки сервера или стёртого по ошибке
каталога копия только докладывается и чужую историю не трогает.
Для rsync один раз зайти туда руками от root (`ssh user@host`) и принять
отпечаток: ночью спросить будет некого, и копия упадёт.

В дампах телефоны людей, в архиве `media` — приложенные к обращениям
файлы. Чужому хранилищу их лучше отдавать зашифрованными: для rclone —
remote типа `crypt` поверх облака, для rsync — диск или хранилище,
которое шифрует само.

Сервер пересобран — сначала вернуть `/var/backups/sayr` с той стороны,
потом включать таймер.

```bash
cp deploy/sayr-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now sayr-backup.timer
systemctl start sayr-backup && journalctl -u sayr-backup -n 30 --no-pager
```

Восстановление — в пустую базу:
`gzip -dc /var/backups/sayr/daily/db-ГГГГ-ММ-ДД.sql.gz | sudo -u postgres psql sayr`.

Перед миграциями `update.sh` снимает свой дамп,
`/var/backups/sayr/pre-deploy-ГГГГММДД-ЧЧММСС.sql.gz`, и держит 10 последних.
Не вышел дамп — деплой останавливается до миграций, код возвращается на
прежний коммит.
