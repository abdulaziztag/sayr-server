"""Статистика владельца: сбор, ротация, свёртки.

Два потока событий. Серверный: middleware выводит открытия каталога,
мест и треков из запросов, которые и так приходят. Клиентский: приложения
шлют события по перечню из api/events.py и заголовок X-Sayr-App
с платформой и версией. Всё, что показывает страница «Статистика»,
собирает stats_dashboard.py.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs

from datetime import date, datetime, timedelta

from sqlalchemy import Date, cast, delete, distinct, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import settings
from .db import SessionLocal
from .models import (
    ApiEvent,
    CohortRetention,
    DailyCount,
    DailyPlatform,
    DailyStat,
    Device,
    TripIntent,
)

log = logging.getLogger(__name__)

DEVICE_HEADER = b"x-device-id"
APP_HEADER = b"x-sayr-app"

# Метка канала из ссылки лендинга (/?from=gorets): до 32 знаков безобидного
# алфавита, потому что значение попадает в базу. Один очиститель на
# middleware и на редирект в магазины — чтобы метка в обоих местах
# нормализовалась одинаково
_MARK_MAX = 32


def clean_mark(raw: str | None) -> str | None:
    if not raw:
        return None
    return "".join(c for c in raw[:_MARK_MAX] if c.isalnum() or c in "-_") or None


@dataclass(frozen=True)
class AppInfo:
    """Заголовок X-Sayr-App: `android/1.7.0 ru 14` — платформа, версия,
    язык интерфейса, старшая версия системы. Города здесь нет намеренно."""

    platform: str
    version: str
    lang: str | None
    os_major: str | None
    debug: bool


_PLATFORMS = {"ios", "android"}
_APP_VERSION = re.compile(r"^\d+(\.\d+){0,3}(-debug)?$")


def parse_app_header(value: str | None) -> AppInfo | None:
    """Разбор заголовка; всё, что не по форме, — None, а не исключение:
    заголовок ставят клиенты, и кривой заголовок не должен ронять запрос."""
    if not value:
        return None
    parts = value.strip().split()
    if not parts or "/" not in parts[0]:
        return None
    platform, _, version = parts[0].partition("/")
    platform = platform.lower()
    if platform not in _PLATFORMS or not _APP_VERSION.match(version):
        return None
    debug = version.endswith("-debug")
    if debug:
        version = version[: -len("-debug")]
    lang = None
    if len(parts) > 1 and parts[1].isalpha() and len(parts[1]) == 2:
        lang = parts[1].lower()
    os_major = None
    if len(parts) > 2 and parts[2].replace(".", "").isdigit():
        os_major = parts[2][:8]
    return AppInfo(platform, version[:16], lang, os_major, debug)

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
        return kind, clean_mark(parse_qs(query).get("from", [""])[0])
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
        app_header = None
        for name, value in scope.get("headers", []):
            if name == DEVICE_HEADER:
                device = value.decode("latin-1", "ignore").strip()[:64] or None
            elif name == APP_HEADER:
                app_header = value.decode("latin-1", "ignore")
        info = parse_app_header(app_header)
        # Отладочные сборки не считаются: эмулятор и симулятор ходят на боевой
        # сервер, и каждая проверка ложилась в статистику живым человеком
        if info is not None and info.debug and not settings.stats_count_debug:
            return

        # Ответ уже ушёл через send_wrapper — эта запись человека не задержит.
        # Именно await, а не create_task: задача без ссылки на неё может быть
        # собрана сборщиком мусора на полпути, и событие просто пропадёт
        await _record(event[0], event[1], device, info)


async def touch_device(
    session: AsyncSession, device: str, info: AppInfo | None, today: date | None = None
) -> None:
    """Первое появление — вставить; заголовок — записать не чаще раза в день.

    Раз в день, а не на каждый запрос: за сутки устройство делает десятки
    обращений, и каждое обновляло бы строку ради тех же значений.
    """
    today = today or date.today()
    await session.execute(
        insert(Device)
        .values(device=device, first_seen=today)
        .on_conflict_do_nothing(index_elements=[Device.device])
    )
    if info is None:
        return
    await session.execute(
        update(Device)
        .where(
            Device.device == device,
            or_(Device.last_seen.is_(None), Device.last_seen < today),
        )
        .values(
            platform=info.platform,
            app_version=info.version,
            lang=info.lang,
            os_major=info.os_major,
            last_seen=today,
        )
    )


async def record_event(
    kind: str, slug: str | None, device: str | None, info: AppInfo | None = None
) -> None:
    """Записать событие вне middleware — редирект в магазины пишет клик
    по каналу сам. Тот же путь, те же гарантии: ошибка не роняет запрос."""
    await _record(kind, slug, device, info)


async def _record(
    kind: str, slug: str | None, device: str | None, info: AppInfo | None = None
) -> None:
    try:
        async with SessionLocal() as session:
            session.add(ApiEvent(kind=kind, slug=slug, device=device))
            if device:
                await touch_device(session, device, info)
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
        await rollup_day(session, day)
    await session.commit()

    await rotate_weeks(session, today)
    await purge(session, today)


async def rollup_day(session: AsyncSession, day: date) -> None:
    """Универсальная свёртка одного дня: `daily_counts` и `daily_platform`.

    Не знает видов: группирует сырьё по `kind` и `key`, какие бы виды
    ни появились. Перезаписывает, а не прибавляет (`ON CONFLICT DO UPDATE`):
    повторный прогон того же дня — досчёт, второй воркер, ручной
    `stats_backfill` — обязан давать те же числа, а не удвоенные.
    """
    same_day = cast(ApiEvent.ts, Date) == day
    key = func.coalesce(ApiEvent.slug, "")

    rows = (
        await session.execute(
            select(
                ApiEvent.kind,
                key,
                func.count(),
                func.count(distinct(ApiEvent.device)),
            )
            .where(same_day)
            .group_by(ApiEvent.kind, key)
        )
    ).all()
    for kind, k, events, devices in rows:
        stmt = insert(DailyCount).values(day=day, kind=kind, key=k, events=events, devices=devices)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[DailyCount.day, DailyCount.kind, DailyCount.key],
                set_={"events": stmt.excluded.events, "devices": stmt.excluded.devices},
            )
        )

    platform = func.coalesce(Device.platform, "unknown")
    active_rows = (
        await session.execute(
            select(platform, func.count(distinct(ApiEvent.device)))
            .select_from(ApiEvent)
            .outerjoin(Device, Device.device == ApiEvent.device)
            .where(same_day, ApiEvent.device.is_not(None))
            .group_by(platform)
        )
    ).all()
    new_rows = dict(
        (
            await session.execute(
                select(platform, func.count())
                .select_from(Device)
                .where(Device.first_seen == day)
                .group_by(platform)
            )
        ).all()
    )
    for plat, active in active_rows:
        stmt = insert(DailyPlatform).values(
            day=day, platform=plat, active=active, new=new_rows.get(plat, 0)
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[DailyPlatform.day, DailyPlatform.platform],
                set_={"active": stmt.excluded.active, "new": stmt.excluded.new},
            )
        )


def week_start(day: date) -> date:
    """Понедельник недели, в которую попадает день."""
    return day - timedelta(days=day.weekday())


async def rotate_weeks(session: AsyncSession, today: date | None = None) -> None:
    """Когорты по закрытым неделям, которых ещё нет в `cohort_retention`.

    Закрытая неделя — та, чьё воскресенье уже прошло. Считать её можно,
    пока сырьё за неё целиком в окне хранения: 30 дней больше семи,
    так что окно даёт две-три закрытые недели с запасом, а более старые
    не досчитываются и не притворяются нулями.
    """
    today = today or date.today()
    this_monday = week_start(today)
    oldest_raw = today - timedelta(days=settings.stats_retention_days)

    counted = set(
        (
            await session.execute(
                select(CohortRetention.cohort_week, CohortRetention.week_index)
            )
        ).all()
    )
    weeks_done = {cw + timedelta(weeks=idx) for cw, idx in counted}

    monday = this_monday - timedelta(weeks=1)
    while monday >= oldest_raw:
        if monday not in weeks_done:
            await _rollup_week(session, monday)
        monday -= timedelta(weeks=1)
    await session.commit()


async def _rollup_week(session: AsyncSession, monday: date) -> None:
    sunday_end = monday + timedelta(weeks=1)
    cohort = cast(func.date_trunc("week", Device.first_seen), Date)
    rows = (
        await session.execute(
            select(cohort, func.count(distinct(ApiEvent.device)))
            .select_from(ApiEvent)
            .join(Device, Device.device == ApiEvent.device)
            .where(
                cast(ApiEvent.ts, Date) >= monday,
                cast(ApiEvent.ts, Date) < sunday_end,
            )
            .group_by(cohort)
        )
    ).all()
    for cohort_week, devices in rows:
        index = (monday - cohort_week).days // 7
        if index < 0:
            continue
        stmt = insert(CohortRetention).values(
            cohort_week=cohort_week, week_index=index, devices=devices
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[CohortRetention.cohort_week, CohortRetention.week_index],
                set_={"devices": stmt.excluded.devices},
            )
        )


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
