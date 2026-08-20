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

    weather_cache_ttl_sec: int = 30 * 60

    # Сколько дней держим сырые события статистики. Дневные агрегаты живут
    # вечно, стирается только сырьё. Тридцать, а не больше: столько же
    # обещано на /privacy для технических журналов, и два разных срока
    # означали бы, что в одном из мест мы врём
    stats_retention_days: int = 30

    model_config = {"env_file": SERVER_DIR / ".env", "env_prefix": "SAYR_"}


settings = Settings()

PHOTOS_DIR = settings.media_dir / "photos"
THUMBS_DIR = settings.media_dir / "thumbs"
GPX_DIR = settings.media_dir / "gpx"

# Куда уезжают снимки, удалённые из админки. Намеренно вне media_dir:
# по /media отдаётся всё содержимое, и корзина внутри означала бы, что
# «удалённое» фото по-прежнему открывается по прямой ссылке.
#
# Не удаляем совсем, потому что цена ошибки несимметрична: лишний файл
# на диске — ничто, а промах по кнопке стоит снимка, который искали
# вручную по выгрузке форума и второй раз можем не найти.
DELETED_PHOTOS_DIR = SERVER_DIR / "deleted-photos"

# StaticFiles и FileSystemStorage требуют существующих директорий уже на импорте
for _d in (PHOTOS_DIR, THUMBS_DIR, GPX_DIR):
    _d.mkdir(parents=True, exist_ok=True)
