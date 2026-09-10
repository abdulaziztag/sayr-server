import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.staticfiles import StaticFiles

from .admin import mount_admin
from .api import (intents, landing, legal, places, regions, report, seasons,
                  seasons_review, share, drive_times, push, app_update)
from .config import GPX_DIR, PHOTOS_DIR, SERVER_DIR, THUMBS_DIR, settings
from .db import engine
from .stats import StatsMiddleware, rotate_forever


@asynccontextmanager
async def lifespan(app: FastAPI):
    rotation = asyncio.create_task(rotate_forever())
    yield
    # Снимаем ДО dispose: иначе цикл проснётся над закрытым пулом
    rotation.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await rotation
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


# Самый внешний слой: add_middleware вставляет в начало списка, а событие
# пишется по итоговому статусу ответа — включая 304 от StaticFiles
app.add_middleware(StatsMiddleware)

# Три каталога поимённо, а не media_dir целиком. Рядом с ними на том же
# томе лежит media/reports — файлы, приложенные к заявкам с формы. Это
# чужие снимки, присланные незнакомыми людьми, и монтирование корня
# раздавало бы их по прямой ссылке всякому, кто её угадает
app.mount("/media/photos", StaticFiles(directory=PHOTOS_DIR), name="media-photos")
app.mount("/media/thumbs", StaticFiles(directory=THUMBS_DIR), name="media-thumbs")
app.mount("/media/gpx", StaticFiles(directory=GPX_DIR), name="media-gpx")
# Шрифты и картинки лендинга. Отдельно от media: то — пользовательский
# контент, это — часть страницы, и живёт вместе с кодом
app.mount("/static", StaticFiles(directory=SERVER_DIR / "static"), name="static")
app.include_router(places.router)
app.include_router(regions.router)
app.include_router(drive_times.router)
app.include_router(intents.router)
app.include_router(share.router)
app.include_router(legal.router)
app.include_router(push.router)
app.include_router(app_update.router)
app.include_router(report.router)
app.include_router(seasons.router)
app.include_router(seasons_review.router)
# Лендинг последним: его "/" не должен перехватывать ничего выше
app.include_router(landing.router)
mount_admin(app)


@app.get("/healthz", tags=["meta"])
async def healthz():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok"}
