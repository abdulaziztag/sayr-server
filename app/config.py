from pathlib import Path

from pydantic_settings import BaseSettings

SERVER_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://sayr:sayr@localhost:5432/sayr"
    media_dir: Path = SERVER_DIR / "media"
    admin_username: str = "admin"

    # Без значений по умолчанию — приложение падает на старте, пока их не задали.
    # Это намеренно: с известным secret_key админку открывают подделанной cookie
    # `session={"admin": true}`, минуя форму входа. Дефолт, лежащий в открытом
    # репозитории, означал бы открытую панель на любом сервере, где забыли .env.
    admin_password: str
    secret_key: str

    # Cookie админки только по HTTPS. Локально по http поставь false в .env
    admin_cookie_secure: bool = True

    # Пусто — CORS выключен. Нативным клиентам он не нужен, список понадобится,
    # только если появится веб-морда: SAYR_CORS_ORIGINS=["https://sayr.uz"]
    cors_origins: list[str] = []

    # Адреса магазинов для лендинга. Пока пусты — страница честно пишет
    # «скоро»: она поднимается раньше, чем приложения проходят ревью,
    # а ссылка в никуда хуже отсутствующей
    app_store_url: str = ""
    play_store_url: str = ""

    weather_cache_ttl_sec: int = 30 * 60

    # Сколько дней держим сырые события статистики. Дневные агрегаты живут
    # вечно, стирается только сырьё. Тридцать, а не больше: столько же
    # обещано на /privacy для технических журналов, и два разных срока
    # означали бы, что в одном из мест мы врём
    stats_retention_days: int = 30

    # Пуши. Пустой путь — платформа не настроена: планировщик пропустит её
    # устройства и запишет это в last_error объявления, а не упадёт целиком.
    # APNs — ключ .p8 из Apple Developer и его Key ID; тема — bundle id
    apns_key_path: Path | None = None
    apns_key_id: str = ""
    apns_team_id: str = "Z39Z5TJZCG"
    apns_topic: str = "uz.sayr.ios"
    # Песочница APNs принимает токены только debug-сборок из Xcode;
    # TestFlight и App Store ходят в боевой
    apns_sandbox: bool = False
    # FCM — сервисный ключ проекта Firebase (JSON)
    fcm_service_account_path: Path | None = None

    model_config = {"env_file": SERVER_DIR / ".env", "env_prefix": "SAYR_"}


settings = Settings()

PHOTOS_DIR = settings.media_dir / "photos"
THUMBS_DIR = settings.media_dir / "thumbs"
GPX_DIR = settings.media_dir / "gpx"

# Файлы, приложенные к заявкам с формы /report. Лежат внутри media_dir,
# потому что это единственный каталог на томе docker: всё остальное внутри
# контейнера стирается пересборкой, а фотография несошедшегося поворота
# нужна ровно тогда, когда до заявки дошли руки, — месяцем позже.
#
# При этом наружу они НЕ отдаются: в main.py под /media смонтированы три
# каталога поимённо, а не media_dir целиком. Это чужие файлы, присланные
# незнакомыми людьми: там бывает и лишнее в кадре, и просто мусор, и
# публичная ссылка на sayr.info превратила бы форму в файлохостинг.
# Владельцу они открываются через админку, по сессии
REPORTS_DIR = settings.media_dir / "reports"

# Куда уезжают снимки, удалённые из админки. Внутри media_dir — рядом
# с photos, из которого их и переносят, и это обязательное условие,
# а не вкусовщина.
#
# Раньше корзина лежала в SERVER_DIR, «подальше от раздаваемого». Но юнит
# идёт с ProtectSystem=strict и ReadWritePaths=<...>/media, а systemd
# выполняет ReadWritePaths bind-монтированием: внутри службы media —
# отдельная точка монтирования. rename() через границу монтирования
# отказывает с EXDEV, даже когда файловая система под ней одна и та же.
# С 24 по 26 августа так молча потерялись 55 снимков: строки из базы
# ушли, файлы остались лежать в photos и открываться по прямой ссылке.
#
# Наружу каталог при этом не выходит: в main.py под /media смонтированы
# три подкаталога поимённо (photos, thumbs, gpx), а не media_dir целиком.
# Той же защитой закрыт и REPORTS_DIR выше.
#
# Не удаляем совсем, потому что цена ошибки несимметрична: лишний файл
# на диске — ничто, а промах по кнопке стоит снимка, который искали
# вручную по выгрузке форума и второй раз можем не найти.
DELETED_PHOTOS_DIR = settings.media_dir / "deleted-photos"

# StaticFiles и FileSystemStorage требуют существующих директорий уже на импорте
for _d in (PHOTOS_DIR, THUMBS_DIR, GPX_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
