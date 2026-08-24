"""Статистика владельца: сбор, ротация, агрегации для дашборда.

Событий с клиента нет — сервер выводит всё из запросов, которые к нему
и так приходят. Единственное, что делают приложения, — шлют заголовок
X-Device-Id со своим случайным идентификатором.
"""

import asyncio
import logging
from urllib.parse import parse_qs

from datetime import date, datetime, timedelta

from sqlalchemy import Date, cast, delete, distinct, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import settings
from .db import SessionLocal
from .models import ApiEvent, DailyStat, Device, Place, PlaceTrack, TripIntent

log = logging.getLogger(__name__)

DEVICE_HEADER = b"x-device-id"

# Маршруты, которые считаем. Ключ — path из scope["route"], а не префикс
# пути: у /places/{slug} есть соседи /weather и /intents, и префиксный
# матчинг записал бы запрос погоды открытием места
ROUTES = {
    "/api/v1/places": "catalog",
    "/api/v1/places/{slug}": "place",
    "/p/{slug}": "share",
    "/": "landing",
    "/uz": "landing",
}


def _kind_and_slug(scope: Scope) -> tuple[str, str | None] | None:
    """Что за событие, если этот запрос вообще считаем."""
    if scope.get("method") != "GET":
        return None

    path = scope.get("path", "")
    # У смонтированного StaticFiles маршрута в scope нет — матчим по пути.
    # Именно .gpx: рядом по /media лежат фотографии, они не в счёт
    if path.startswith("/media/gpx/") and path.endswith(".gpx"):
        return "gpx", path.rsplit("/", 1)[-1]

    route = scope.get("route")
    kind = ROUTES.get(getattr(route, "path", None))
    if kind is None:
        return None
    if kind == "landing":
        # Метка канала вместо слага: ссылку в каждый канал даём со своей
        # (/?from=gorets), и тогда видно не «пришло двести человек»,
        # а откуда именно. Чужое в параметре не пускаем дальше 32 знаков
        # безобидного алфавита — это значение попадает в базу
        query = scope.get("query_string", b"").decode("latin-1", "ignore")
        mark = parse_qs(query).get("from", [""])[0][:32]
        return kind, "".join(c for c in mark if c.isalnum() or c in "-_") or None
    return kind, scope.get("path_params", {}).get("slug")


class StatsMiddleware:
    """Пишет событие после успешного ответа.

    Чистый ASGI, а не BaseHTTPMiddleware: нужен доступ к scope["route"],
    который FastAPI кладёт туда уже после матчинга маршрута.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        # Не 200, а «меньше 400»: StaticFiles отдаёт 304 закэшировавшему
        # клиенту и 206 на догрузку куска — это тоже скачивания
        if status >= 400:
            return
        event = _kind_and_slug(scope)
        if event is None:
            return

        device = None
        for name, value in scope.get("headers", []):
            if name == DEVICE_HEADER:
                device = value.decode("latin-1", "ignore").strip()[:64] or None
                break

        # Ответ уже ушёл через send_wrapper — эта запись человека не задержит.
        # Именно await, а не create_task: задача без ссылки на неё может быть
        # собрана сборщиком мусора на полпути, и событие просто пропадёт
        await _record(event[0], event[1], device)


async def _record(kind: str, slug: str | None, device: str | None) -> None:
    try:
        async with SessionLocal() as session:
            session.add(ApiEvent(kind=kind, slug=slug, device=device))
            if device:
                # Первое появление устройства; повторные — молча мимо
                await session.execute(
                    insert(Device)
                    .values(device=device, first_seen=date.today())
                    .on_conflict_do_nothing(index_elements=[Device.device])
                )
            await session.commit()
    except Exception:  # noqa: BLE001 — статистика не стоит упавшего запроса
        log.warning("не записал событие статистики", exc_info=True)


# MARK: - Ротация


async def rotate(session: AsyncSession, today: date | None = None) -> None:
    """Досчитать агрегаты за закрытые дни и стереть сырьё старше срока.

    Две транзакции, а не одна. Раньше удаление стояло следом за вставками
    в общей транзакции, и когда второй воркер проигрывал гонку за строку
    daily_stats, откатывалась вместе с ней и чистка сырья — то есть падал
    ровно тот шаг, ради которого всё и затевалось. На бою это случилось
    19 августа: UniqueViolation по daily_stats_pkey.
    """
    today = today or date.today()

    done = set(
        (await session.execute(select(DailyStat.day).where(DailyStat.day < today)))
        .scalars()
        .all()
    )
    days = (
        (
            await session.execute(
                select(distinct(cast(ApiEvent.ts, Date))).where(
                    cast(ApiEvent.ts, Date) < today
                )
            )
        )
        .scalars()
        .all()
    )
    for day in sorted(set(days) - done):
        # on_conflict_do_nothing: юнит поднимает uvicorn с двумя воркерами,
        # и каждый крутит свою ротацию. Проигравший молча проходит мимо
        # вместо того, чтобы уронить транзакцию
        await session.execute(
            insert(DailyStat)
            .values(**await _totals(session, day))
            .on_conflict_do_nothing(index_elements=[DailyStat.day])
        )
    await session.commit()

    await purge(session, today)


async def purge(session: AsyncSession, today: date | None = None) -> None:
    """Стереть всё, что политика обещает не хранить дольше срока.

    Политика (api/legal.py) обещает три вещи: записи статистики живут
    не дольше 30 дней, дальше остаются «только общие числа по дням, без
    привязки к устройствам», и записи, привязанные к случайному номеру
    устройства, стираются сами. Значит чистить надо не только события,
    но и сам номер устройства, и прошедшие отметки «пойду» — их человек
    снять уже не может, приложение показывает только будущие даты.
    """
    today = today or date.today()
    cutoff_day = today - timedelta(days=settings.stats_retention_days)
    cutoff_ts = datetime.now().astimezone() - timedelta(days=settings.stats_retention_days)

    await session.execute(delete(ApiEvent).where(ApiEvent.ts < cutoff_ts))

    # Прошедшие отметки. Порог тот же, а не «всё прошедшее»: колонка votes
    # в топе мест считает голоса за последние 7 и 30 дней, и рубить их
    # раньше срока значило бы обеднить дашборд без выигрыша для приватности
    await session.execute(delete(TripIntent).where(TripIntent.day < cutoff_day))

    # Номер устройства. Удаляем только те, которых уже нет в оставшихся
    # событиях: строка нужна, пока по ней считается «новое устройство»
    # за день. Порядок важен — события чистятся выше, поэтому здесь
    # остаются ровно те, кто заходил за последние 30 дней
    await session.execute(
        delete(Device).where(
            Device.first_seen < cutoff_day,
            ~select(ApiEvent.device)
            .where(ApiEvent.device == Device.device)
            .exists(),
        )
    )
    await session.commit()


async def _totals(session: AsyncSession, day: date) -> dict:
    """Числа одного дня — из сырых событий."""
    same_day = cast(ApiEvent.ts, Date) == day

    async def count(*where) -> int:
        return (
            await session.execute(select(func.count()).select_from(ApiEvent).where(*where))
        ).scalar_one()

    active = (
        await session.execute(
            select(func.count(distinct(ApiEvent.device))).where(
                same_day, ApiEvent.device.is_not(None)
            )
        )
    ).scalar_one()
    new = (
        await session.execute(
            select(func.count()).select_from(Device).where(Device.first_seen == day)
        )
    ).scalar_one()

    return {
        "day": day,
        "active_devices": active,
        "new_devices": new,
        "place_opens": await count(same_day, ApiEvent.kind == "place"),
        "catalog_opens": await count(same_day, ApiEvent.kind == "catalog"),
        "gpx_downloads": await count(same_day, ApiEvent.kind == "gpx"),
    }


#: Как часто просыпаться. Час, а не сутки: раньше цикл спал 24 часа и
#: отсчёт обнулялся при каждом рестарте — за 19 дней сервис перезапускали
#: 37 раз, то есть чистка не выполнялась почти никогда. Работы у неё нет,
#: пока нечего чистить, так что ежечасный холостой проход ничего не стоит,
#: а рестарт стоит теперь час задержки вместо суток
ROTATE_EVERY = timedelta(hours=1)


async def rotate_forever() -> None:
    """Цикл ротации. Сначала спит, потом работает.

    Порядок важен: тестовая фикстура клиента прогоняет lifespan на каждом
    тесте, и проход «сразу на старте» ходил бы в базу на каждом тесте.
    Заодно старт приложения не ждёт работы с базой.

    Надёжнее было бы вынести это в systemd-таймер, дёргающий отдельную
    команду: тогда чистка не зависит от процесса вообще. Но таймер живёт
    на сервере, где стоят чужие боевые сайты, и ставить его — отдельное
    решение; ежечасный цикл чинит наблюдавшуюся поломку целиком и едет
    обычным деплоем.
    """
    while True:
        await asyncio.sleep(ROTATE_EVERY.total_seconds())
        try:
            async with SessionLocal() as session:
                await rotate(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — приложение важнее ротации
            log.warning("ротация статистики не прошла", exc_info=True)


# MARK: - Числа для дашборда


async def dashboard(session: AsyncSession) -> dict:
    """Всё, что показывает страница «Статистика», одним запросом данных."""
    today = date.today()

    async def active_since(days: int) -> int:
        return (
            await session.execute(
                select(func.count(distinct(ApiEvent.device))).where(
                    cast(ApiEvent.ts, Date) > today - timedelta(days=days),
                    ApiEvent.device.is_not(None),
                )
            )
        ).scalar_one()

    async def active_on(day: date) -> int:
        stored = (
            await session.execute(
                select(DailyStat.active_devices).where(DailyStat.day == day)
            )
        ).scalar_one_or_none()
        if stored is not None:
            return stored
        return (
            await session.execute(
                select(func.count(distinct(ApiEvent.device))).where(
                    cast(ApiEvent.ts, Date) == day, ApiEvent.device.is_not(None)
                )
            )
        ).scalar_one()

    total_devices = await _devices_ever(session)
    new_week = (
        await session.execute(
            select(func.count())
            .select_from(Device)
            .where(Device.first_seen > today - timedelta(days=7))
        )
    ).scalar_one()

    return {
        "today": today,
        "active_today": await active_on(today),
        "active_yesterday": await active_on(today - timedelta(days=1)),
        "wau": await active_since(7),
        "mau": await active_since(30),
        "new_week": new_week,
        "total_devices": total_devices,
        "days": await _recent_days(session, today),
        "top_week": await _top_places(session, today, 7),
        "top_month": await _top_places(session, today, 30),
        "upcoming": await _upcoming(session, today),
        "shares": await _shares(session, today),
    }


async def _devices_ever(session: AsyncSession) -> int:
    """Сколько устройств видели за всю историю.

    Считать строки в devices больше нельзя: purge() чистит их вместе
    с остальным сырьём, и число превратилось бы в «за последние 30 дней».
    Складываем вместо этого дневные new_devices — они обезличены, живут
    вечно и политике не противоречат, — а сверху добавляем тех, кто
    появился после последнего досчитанного дня.
    """
    historic = (
        await session.execute(select(func.coalesce(func.sum(DailyStat.new_devices), 0)))
    ).scalar_one()
    last_day = (await session.execute(select(func.max(DailyStat.day)))).scalar_one()
    # Ещё не было ни одного закрытого дня — тогда вся правда в devices
    where = () if last_day is None else (Device.first_seen > last_day,)
    fresh = (
        await session.execute(select(func.count()).select_from(Device).where(*where))
    ).scalar_one()
    return historic + fresh


async def _recent_days(session: AsyncSession, today: date) -> list[dict]:
    """Две недели построчно: прошлое из агрегатов, сегодня — на лету."""
    since = today - timedelta(days=13)
    stored = {
        row.day: row
        for row in (
            await session.execute(select(DailyStat).where(DailyStat.day >= since))
        ).scalars()
    }
    rows = []
    for offset in range(14):
        day = today - timedelta(days=offset)
        row = stored.get(day)
        rows.append(
            {
                "day": day,
                "active_devices": row.active_devices,
                "new_devices": row.new_devices,
                "place_opens": row.place_opens,
                "catalog_opens": row.catalog_opens,
                "gpx_downloads": row.gpx_downloads,
            }
            if row
            else await _totals(session, day)
        )
    return rows


async def _top_places(session: AsyncSession, today: date, days: int) -> list[dict]:
    """Что открывают: слаг, имя, открытия, уникальные, голоса, скачивания."""
    since = today - timedelta(days=days)

    opens = (
        await session.execute(
            select(
                ApiEvent.slug,
                func.count().label("opens"),
                func.count(distinct(ApiEvent.device)).label("devices"),
            )
            .where(
                ApiEvent.kind == "place",
                ApiEvent.slug.is_not(None),
                cast(ApiEvent.ts, Date) > since,
            )
            .group_by(ApiEvent.slug)
            .order_by(func.count().desc())
            .limit(25)
        )
    ).all()
    if not opens:
        return []

    slugs = [row.slug for row in opens]
    names = dict(
        (
            await session.execute(select(Place.slug, Place.name).where(Place.slug.in_(slugs)))
        ).all()
    )

    votes = dict(
        (
            await session.execute(
                select(Place.slug, func.count())
                .join(TripIntent, TripIntent.place_id == Place.id)
                .where(Place.slug.in_(slugs), TripIntent.day > since)
                .group_by(Place.slug)
            )
        ).all()
    )

    # Скачивание знает только имя файла: gpx_url у трека — вычисляемое
    # свойство поверх gpx_file, а не колонка, искать надо по хранилищу
    files = dict(
        (
            await session.execute(
                select(PlaceTrack.gpx_file, Place.slug)
                .join(Place, Place.id == PlaceTrack.place_id)
                .where(Place.slug.in_(slugs))
            )
        ).all()
    )
    downloads: dict[str, int] = {}
    if files:
        for name, count in (
            await session.execute(
                select(ApiEvent.slug, func.count())
                .where(ApiEvent.kind == "gpx", cast(ApiEvent.ts, Date) > since)
                .group_by(ApiEvent.slug)
            )
        ).all():
            slug = files.get(name)
            if slug:
                downloads[slug] = downloads.get(slug, 0) + count

    return [
        {
            "slug": row.slug,
            "name": names.get(row.slug, row.slug),
            "opens": row.opens,
            "devices": row.devices,
            "votes": votes.get(row.slug, 0),
            "downloads": downloads.get(row.slug, 0),
        }
        for row in opens
    ]


async def _upcoming(session: AsyncSession, today: date) -> list[dict]:
    """Кто куда собирается — живое из голосов «пойду»."""
    return [
        {"day": row.day, "name": row.name, "people": row.people}
        for row in (
            await session.execute(
                select(
                    TripIntent.day,
                    Place.name,
                    func.count().label("people"),
                )
                .join(Place, Place.id == TripIntent.place_id)
                .where(TripIntent.day >= today)
                .group_by(TripIntent.day, Place.name)
                .order_by(TripIntent.day, func.count().desc())
                .limit(40)
            )
        ).all()
    ]


async def _shares(session: AsyncSession, today: date) -> list[dict]:
    """Работает ли «поделиться»: открытия /p/{slug} за месяц."""
    since = today - timedelta(days=30)
    rows = (
        await session.execute(
            select(ApiEvent.slug, func.count().label("opens"))
            .where(
                ApiEvent.kind == "share",
                ApiEvent.slug.is_not(None),
                cast(ApiEvent.ts, Date) > since,
            )
            .group_by(ApiEvent.slug)
            .order_by(func.count().desc())
            .limit(20)
        )
    ).all()
    if not rows:
        return []
    names = dict(
        (
            await session.execute(
                select(Place.slug, Place.name).where(
                    Place.slug.in_([row.slug for row in rows])
                )
            )
        ).all()
    )
    return [
        {"slug": row.slug, "name": names.get(row.slug, row.slug), "opens": row.opens}
        for row in rows
    ]
