import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import exc, text
from starlette.staticfiles import StaticFiles

from .admin import mount_admin
from .api import (auth, auth_providers, intents, events, landing, legal, me, places, regions,
                  report, seasons, seasons_review, share, sync, drive_times, push, app_update,
                  rooms, room_page)
from .config import AVATARS_DIR, GPX_DIR, PHOTOS_DIR, SERVER_DIR, THUMBS_DIR, settings
from .db import engine
from .stats import StatsMiddleware, rotate_forever

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    rotation = asyncio.create_task(rotate_forever())
    yield
    # Снимаем ДО dispose: иначе цикл проснётся над закрытым пулом
    rotation.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await rotation
    await engine.dispose()


# Схема и её страницы — только локально (settings.api_docs): на бою
# /openapi.json раздавал всё API, включая выключенные флагами ручки
_docs = {} if settings.api_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="Sayr API", version="0.1.0", lifespan=lifespan, **_docs)


@app.exception_handler(exc.DataError)
@app.exception_handler(psycopg.DataError)
async def bad_input(request, error):
    """Мусор во входе, который база не принимает: NUL-байт в строке
    (/api/v1/places/a%00b), число за пределами колонки. Раньше это была
    пятисотка — ошибка сервера там, где ошибся запрос. Проверки по ручкам
    ловят такое раньше; это страховка для тех мест, где проверки нет.

    DataError — весь класс 22 в Postgres, и под ним же прячется настоящая
    ошибка сервера: деление на ноль в подсчёте, слишком длинное значение,
    которое сервер вычислил сам. Раньше её было видно пятисоткой с трассой
    в журнале — теперь только этой строкой, поэтому она обязательна.
    Значений не пишем: там чужой ввод. Путь — через repr, NUL из него
    превратил бы строку журнала в двоичный мусор"""
    cause = getattr(error, "orig", None) or error
    log.warning("мусор во входе: %s %r — %s", request.method, request.url.path, type(cause).__name__)
    return JSONResponse({"detail": "bad_input"}, status_code=422)


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


#: Страницы, которые нельзя показывать в чужой рамке: в них входят
#: паролем и нажимают кнопки, меняющие базу. Подложить такую страницу
#: прозрачным слоем под чужую кнопку — и владелец одобрит или удалит
#: не то. Остальной сайт встраивать некому, но и незачем его запрещать
_NO_FRAMES = ("/admin", "/seasons/review")


@app.middleware("http")
async def security_headers(request, call_next):
    """nosniff — всем: браузер не должен «угадывать» тип файла наперекор
    заголовку, а среди файлов есть присланные незнакомыми людьми.
    HSTS — только тому, кто пришёл по https (nginx сообщает это
    X-Forwarded-Proto): браузер запомнит, что сюда только так, и адрес,
    набранный руками без https, больше не уходит открытым запросом,
    который по дороге можно подменить"""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith(_NO_FRAMES):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
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
# Фото из анкет. Отдаются так же открыто, как снимки мест: имя файла
# случайное, угадать его нельзя, а показывать фото попутчику надо
app.mount("/media/avatars", StaticFiles(directory=AVATARS_DIR), name="media-avatars")
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
app.include_router(events.router)
app.include_router(auth.router)
app.include_router(auth_providers.router)
app.include_router(auth_providers.me_router)
app.include_router(me.router)
app.include_router(sync.router)
app.include_router(rooms.router)
app.include_router(room_page.router)
app.include_router(report.router)
app.include_router(seasons.router)
app.include_router(seasons_review.router)
# Лендинг последним: его "/" не должен перехватывать ничего выше
app.include_router(landing.router)
admin = mount_admin(app)
# У админки своё приложение Starlette со своим перехватом ошибок: DataError
# из её поиска (?search=a%00b) до bad_input выше не доходил — sqladmin уже
# ответил пятисоткой, и внешний обработчик падал на «ответ уже начат»,
# оставляя в журнале RuntimeError вместо понятной строки
admin.admin.add_exception_handler(exc.DataError, bad_input)
admin.admin.add_exception_handler(psycopg.DataError, bad_input)


@app.get("/healthz", tags=["meta"])
async def healthz():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok"}
