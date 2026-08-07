from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.staticfiles import StaticFiles

from .admin import mount_admin
from .api import intents, places, regions, share
from .config import settings
from .db import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="Sayr API", version="0.1.0", lifespan=lifespan)

# Нативным клиентам CORS не нужен: заголовок проверяет браузер, не URLSession.
# Раньше стояло allow_origins=["*"] — включаем, только если задан список
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

@app.middleware("http")
async def media_cache_headers(request, call_next):
    """Cache-Control для фото и треков: без него клиенты перепроверяли файлы
    при каждом открытии — карточки «мигали» загрузкой. Месяц безопасен:
    при замене файла имя меняется (OVERWRITE_EXISTING_FILES=False)."""
    response = await call_next(request)
    if request.url.path.startswith("/media/"):
        response.headers["Cache-Control"] = "public, max-age=2592000"
    return response


app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")
app.include_router(places.router)
app.include_router(regions.router)
app.include_router(intents.router)
app.include_router(share.router)
mount_admin(app)


@app.get("/healthz", tags=["meta"])
async def healthz():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok"}
