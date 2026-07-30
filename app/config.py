from pathlib import Path

from pydantic_settings import BaseSettings

SERVER_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://sayr:sayr@localhost:5432/sayr"
    media_dir: Path = SERVER_DIR / "media"
    admin_username: str = "admin"
    admin_password: str = "sayr-dev"
    secret_key: str = "dev-secret-change-me"
    weather_cache_ttl_sec: int = 30 * 60

    model_config = {"env_file": SERVER_DIR / ".env", "env_prefix": "SAYR_"}


settings = Settings()

PHOTOS_DIR = settings.media_dir / "photos"
THUMBS_DIR = settings.media_dir / "thumbs"
GPX_DIR = settings.media_dir / "gpx"

# StaticFiles и FileSystemStorage требуют существующих директорий уже на импорте
for _d in (PHOTOS_DIR, THUMBS_DIR, GPX_DIR):
    _d.mkdir(parents=True, exist_ok=True)
