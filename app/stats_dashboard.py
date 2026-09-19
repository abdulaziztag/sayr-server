"""Данные страницы «Статистика»: пять разделов одним вызовом.

Источники — три слоя, у каждого своя честность:

* `daily_stats`, `daily_counts`, `daily_platform`, `cohort_retention` —
  свёртки по дням, живут вечно; отсюда суммы за любой период.
* `api_events` — сырьё за срок хранения (`stats_retention_days`); только
  по нему считаются уникальные устройства, воронка и возврат. Поэтому всё,
  где нужно «сколько устройств», считается за окно `min(период, срок)`,
  и страница об этом говорит прямо.
* сегодня — тоже из сырья, на лету: свёртки закрывают день по его концу.

Клиентские события едут только с обновлённых приложений, и первые недели
их разделы пусты. Пустой блок должен говорить «данные пойдут с версии X»,
а не рисовать ноль как факт — за это отвечает `since_text()`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import Date, Integer, cast, distinct, extract, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import charts
from .api.app_update import parse_version
from .charts import ACCENT, GREEN, MUTED, Series
from .config import settings
from .models import (
    Announcement,
    AnnouncementStatus,
    ApiEvent,
    AppUpdate,
    CohortRetention,
    DailyCount,
    DailyPlatform,
    DailyStat,
    Device,
    Place,
    PlacePaceStats,
    PlaceTrack,
    Region,
    TripIntent,
)
from .stats import _totals, week_start

PERIODS = (7, 30, 90)
DEFAULT_PERIOD = 30
#: Столбцы топа мест, по которым можно сортировать адресом (?sort=nav)
SORTS = {
    "opens": "opens",
    "devices": "devices",
    "votes": "votes",
    "nav": "nav",
    "finish": "finish",
    "rec": "rec",
    "share": "finish_share",
}
#: «Смотрят, но не идут»: открытий не меньше стольких и ни одной навигации
LOOK_MIN_OPENS = 10
#: «Трек под вопросом»: навигаций от пяти, а сходов больше половины
#: или финишей меньше трети
TRACK_MIN_NAV = 5
TRACK_OFFTRAIL_SHARE = 0.5
TRACK_FINISH_SHARE = 1 / 3
TOP_LIMIT = 25

#: С какой версии приложения едут клиентские события. Пусто, пока выпуск
#: не собран, — номера проставляются на этапе выкладки клиентов
CLIENT_EVENTS_SINCE: dict[str, str | None] = {"ios": None, "android": None}

TZ = "Asia/Tashkent"
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
PLATFORM_RU = {"ios": "iOS", "android": "Android", "unknown": "без заголовка"}
CATEGORY_RU = {
    "waterfall": "Водопады",
    "peak": "Вершины",
    "gorge": "Ущелья",
    "cave": "Пещеры",
    "lake": "Озёра",
    "canyon": "Каньоны",
    "spring": "Родники",
    "plateau": "Плато",
    "petroglyphs": "Петроглифы",
    "reserve": "Заповедники",
    "desert": "Пустыни",
    "other": "Другое",
}


def since_text() -> str:
    """Подпись пустого блока: откуда ждать данные."""
    parts = []
    for platform in ("ios", "android"):
        version = CLIENT_EVENTS_SINCE.get(platform)
        parts.append(f"{PLATFORM_RU[platform]} {version}" if version else PLATFORM_RU[platform])
    if any(CLIENT_EVENTS_SINCE.values()):
        return "Данные пойдут с версий " + " и ".join(parts) + "."
    return "Данные пойдут с ближайшего обновления приложения (iOS и Android)."


def pct(part: float, whole: float) -> int | None:
    return round(100 * part / whole) if whole else None


def pct_text(part: float, whole: float) -> str:
    value = pct(part, whole)
    return "—" if value is None else f"{value} %"


@dataclass
class Counts:
    """Суммы событий за период по (вид, ключ): свёртки плюс сегодня из сырья."""

    events: dict[tuple[str, str], int] = field(default_factory=dict)
    device_days: dict[tuple[str, str], int] = field(default_factory=dict)

    def add(self, kind: str, key: str, events: int, devices: int) -> None:
        self.events[(kind, key)] = self.events.get((kind, key), 0) + int(events)
        self.device_days[(kind, key)] = self.device_days.get((kind, key), 0) + int(devices)

    def get(self, kind: str, key: str = "") -> int:
        return self.events.get((kind, key), 0)

    def total(self, kind: str) -> int:
        return sum(v for (k, _), v in self.events.items() if k == kind)

    def by_key(self, kind: str) -> dict[str, int]:
        return {key: v for (k, key), v in self.events.items() if k == kind}

    def has_client_data(self) -> bool:
        """Есть ли обновлённые клиенты: по app_open — его шлёт каждый запуск.

        Не «хоть одно событие из перечня»: одиночная проверка ручкой
        или случайный favorite переключили бы все пустые блоки с «данные
        пойдут с версии» на «таких мест нет», и это было бы враньём.
        """
        return self.total("app_open") > 0


async def dashboard(session: AsyncSession, period_days: int = DEFAULT_PERIOD,
                    sort: str = "opens", today: date | None = None) -> dict:
    """Всё для страницы: обзор, запуск, места, тропа, приложение."""
    today = today or date.today()
    period = period_days if period_days in PERIODS else DEFAULT_PERIOD
    retention = settings.stats_retention_days
    window = min(period, retention)
    sort = sort if sort in SORTS else "opens"

    since = today - timedelta(days=period)
    wsince = today - timedelta(days=window)
    rsince = today - timedelta(days=retention)
    days = [since + timedelta(days=i + 1) for i in range(period)]

    counts = await _counts(session, since, today)
    daily = await _daily(session, since, today, days)
    unique_w = await _unique(session, wsince)
    unique_r = unique_w if window == retention else await _unique(session, rsince)
    intents = await _intents(session, since, wsince, rsince, today)
    cohorts = await _cohorts(session, since)
    client = counts.has_client_data()

    data = {
        "today": today,
        "period": period,
        "window": window,
        "retention": retention,
        "sort": sort,
        "client_data": client,
        "since_text": since_text(),
        "total_devices": await devices_ever(session),
    }
    data["overview"] = _overview(counts, daily, unique_w, intents, cohorts, days, window)
    data["launch"] = await _launch(session, counts, daily, cohorts, days, today, rsince)
    data["places"] = await _places(session, counts, unique_w, intents, sort, since, today, window)
    data["trail"] = await _trail(session, counts, unique_r, intents, rsince, retention)
    data["app"] = await _app(session, counts, daily, days, wsince, today, window)
    return data


# --- источники -------------------------------------------------------------


async def _counts(session: AsyncSession, since: date, today: date) -> Counts:
    counts = Counts()
    for kind, key, events, devices in (
        await session.execute(
            select(
                DailyCount.kind,
                DailyCount.key,
                func.sum(DailyCount.events),
                func.sum(DailyCount.devices),
            )
            .where(DailyCount.day > since, DailyCount.day < today)
            .group_by(DailyCount.kind, DailyCount.key)
        )
    ).all():
        counts.add(kind, key, events, devices)
    # Ключ — одним объектом и в select, и в group_by, а пустая строка литералом:
    # с двумя bind-параметрами Postgres не признаёт выражения одинаковыми
    key = func.coalesce(ApiEvent.slug, literal_column("''"))
    for kind, key_value, events, devices in (
        await session.execute(
            select(ApiEvent.kind, key, func.count(), func.count(distinct(ApiEvent.device)))
            .where(cast(ApiEvent.ts, Date) == today)
            .group_by(ApiEvent.kind, key)
        )
    ).all():
        counts.add(kind, key_value, events, devices)
    return counts


@dataclass
class Daily:
    """Ряды по дням периода, в порядке `days`."""

    active: list[int]
    new: list[int]
    sessions: list[int]
    rec_devices: list[int]
    intent_devices: list[int]
    platforms: dict[str, list[int]]


async def _daily(session: AsyncSession, since: date, today: date, days: list[date]) -> Daily:
    index = {day: i for i, day in enumerate(days)}
    n = len(days)
    active, new = [0] * n, [0] * n
    for row in (
        await session.execute(select(DailyStat).where(DailyStat.day > since))
    ).scalars():
        if row.day in index:
            active[index[row.day]] = row.active_devices
            new[index[row.day]] = row.new_devices
    if today in index:
        live = await _totals(session, today)
        active[index[today]] = live["active_devices"]
        new[index[today]] = live["new_devices"]

    sessions, rec = [0] * n, [0] * n
    for day, kind, events, devices in (
        await session.execute(
            select(
                DailyCount.day,
                DailyCount.kind,
                func.sum(DailyCount.events),
                func.sum(DailyCount.devices),
            )
            .where(
                DailyCount.day > since,
                DailyCount.day < today,
                DailyCount.kind.in_(("app_open", "rec_finish")),
            )
            .group_by(DailyCount.day, DailyCount.kind)
        )
    ).all():
        if day in index:
            (sessions if kind == "app_open" else rec)[index[day]] = int(events if kind == "app_open" else devices)
    for kind, events, devices in (
        await session.execute(
            select(ApiEvent.kind, func.count(), func.count(distinct(ApiEvent.device)))
            .where(cast(ApiEvent.ts, Date) == today, ApiEvent.kind.in_(("app_open", "rec_finish")))
            .group_by(ApiEvent.kind)
        )
    ).all():
        if today in index:
            (sessions if kind == "app_open" else rec)[index[today]] = int(events if kind == "app_open" else devices)

    intents = [0] * n
    for day, devices in (
        await session.execute(
            select(cast(TripIntent.created_at, Date), func.count(distinct(TripIntent.device_id)))
            .where(cast(TripIntent.created_at, Date) > since)
            .group_by(cast(TripIntent.created_at, Date))
        )
    ).all():
        if day in index:
            intents[index[day]] = int(devices)

    platforms: dict[str, list[int]] = defaultdict(lambda: [0] * n)
    for row in (
        await session.execute(
            select(DailyPlatform).where(DailyPlatform.day > since, DailyPlatform.day < today)
        )
    ).scalars():
        if row.day in index:
            platforms[row.platform][index[row.day]] = row.active
    if today in index:
        known = func.coalesce(Device.platform, literal_column("'unknown'"))
        for platform, devices in (
            await session.execute(
                select(known, func.count(distinct(ApiEvent.device)))
                .select_from(ApiEvent)
                .outerjoin(Device, Device.device == ApiEvent.device)
                .where(cast(ApiEvent.ts, Date) == today, ApiEvent.device.is_not(None))
                .group_by(known)
            )
        ).all():
            platforms[platform][index[today]] = int(devices)
    return Daily(active, new, sessions, rec, intents, dict(platforms))


@dataclass
class Unique:
    """Уникальные устройства из сырья за окно: всего и по видам событий."""

    active: int
    by_kind: dict[str, int]
    by_place: dict[str, int]

    def get(self, kind: str) -> int:
        return self.by_kind.get(kind, 0)


async def _unique(session: AsyncSession, since: date) -> Unique:
    where = (cast(ApiEvent.ts, Date) > since, ApiEvent.device.is_not(None))
    active = (
        await session.execute(select(func.count(distinct(ApiEvent.device))).where(*where))
    ).scalar_one()
    by_kind = {
        kind: int(devices)
        for kind, devices in (
            await session.execute(
                select(ApiEvent.kind, func.count(distinct(ApiEvent.device)))
                .where(*where)
                .group_by(ApiEvent.kind)
            )
        ).all()
    }
    by_place = {
        slug: int(devices)
        for slug, devices in (
            await session.execute(
                select(ApiEvent.slug, func.count(distinct(ApiEvent.device)))
                .where(*where, ApiEvent.kind == "place", ApiEvent.slug.is_not(None))
                .group_by(ApiEvent.slug)
            )
        ).all()
    }
    return Unique(int(active), by_kind, by_place)


@dataclass
class Intents:
    """«Пойду»: голоса по местам за период и устройства за окна."""

    votes: dict[str, int]
    devices_window: int
    devices_retention: int
    upcoming: list[dict]


async def _intents(session: AsyncSession, since: date, wsince: date, rsince: date,
                   today: date) -> Intents:
    votes = {
        slug: int(count)
        for slug, count in (
            await session.execute(
                select(Place.slug, func.count())
                .join(TripIntent, TripIntent.place_id == Place.id)
                .where(TripIntent.day > since)
                .group_by(Place.slug)
            )
        ).all()
    }

    async def devices_since(day: date) -> int:
        return int(
            (
                await session.execute(
                    select(func.count(distinct(TripIntent.device_id))).where(
                        cast(TripIntent.created_at, Date) > day
                    )
                )
            ).scalar_one()
        )

    upcoming = [
        {"day": row.day, "name": row.name, "people": row.people}
        for row in (
            await session.execute(
                select(TripIntent.day, Place.name, func.count().label("people"))
                .join(Place, Place.id == TripIntent.place_id)
                .where(TripIntent.day >= today)
                .group_by(TripIntent.day, Place.name)
                .order_by(TripIntent.day, func.count().desc())
                .limit(40)
            )
        ).all()
    ]
    return Intents(votes, await devices_since(wsince), await devices_since(rsince), upcoming)


@dataclass
class Cohorts:
    """Недельные когорты: размер и вернувшиеся по неделям после прихода."""

    weeks: list[date]
    size: dict[date, int]
    returned: dict[tuple[date, int], int]
    max_index: int


async def _cohorts(session: AsyncSession, since: date) -> Cohorts:
    rows = (
        await session.execute(
            select(CohortRetention)
            .where(CohortRetention.cohort_week >= week_start(since))
            .order_by(CohortRetention.cohort_week, CohortRetention.week_index)
        )
    ).scalars().all()
    size = {r.cohort_week: r.devices for r in rows if r.week_index == 0}
    # Когорта без нулевой недели — пришла до начала свёрток (или до срока
    # хранения сырья): её размер неизвестен, и доля от него была бы враньём
    weeks = sorted(size)
    returned = {
        (r.cohort_week, r.week_index): r.devices
        for r in rows
        if r.week_index > 0 and r.cohort_week in size
    }
    max_index = max((i for (_, i) in returned), default=0)
    return Cohorts(weeks, size, returned, max_index)


async def devices_ever(session: AsyncSession) -> int:
    """Сколько устройств видели за всю историю.

    Считать строки в devices нельзя: purge() чистит их вместе с остальным
    сырьём, и число превратилось бы в «за последние 30 дней». Складываем
    дневные new_devices — они обезличены и живут вечно, — а сверху
    добавляем тех, кто появился после последнего досчитанного дня.
    """
    historic = (
        await session.execute(select(func.coalesce(func.sum(DailyStat.new_devices), 0)))
    ).scalar_one()
    last_day = (await session.execute(select(func.max(DailyStat.day)))).scalar_one()
    where = () if last_day is None else (Device.first_seen > last_day,)
    fresh = (
        await session.execute(select(func.count()).select_from(Device).where(*where))
    ).scalar_one()
    return int(historic) + int(fresh)


# --- разделы ---------------------------------------------------------------


def _overview(counts: Counts, daily: Daily, unique: Unique, intents: Intents,
              cohorts: Cohorts, days: list[date], window: int) -> dict:
    active_w = unique.active
    rec_w = unique.get("rec_finish")
    sessions = sum(daily.sessions)

    # Возврат на следующей неделе — по когортам периода, у которых вторая
    # неделя уже закрылась
    closed = [w for w in cohorts.weeks if (w, 1) in cohorts.returned]
    came = sum(cohorts.size.get(w, 0) for w in closed)
    back = sum(cohorts.returned[(w, 1)] for w in closed)
    week_spark = [
        (pct(cohorts.returned[(w, 1)], cohorts.size.get(w, 0)) or 0) for w in closed
    ]

    def share_spark(parts: list[int]) -> list[float]:
        return [
            (100 * p / a if a else 0) for p, a in zip(parts, daily.active)
        ]

    window_note = f"уникальных за {window} дн."
    tiles = [
        {
            "label": "Активные устройства",
            "value": f"{active_w}",
            "note": window_note,
            "spark": charts.sparkline(daily.active),
        },
        {
            "label": "Новые",
            "value": f"{sum(daily.new)}",
            "note": "впервые увидели за период",
            "spark": charts.sparkline(daily.new),
        },
        {
            "label": "Сессии",
            "value": f"{sessions}" if sessions or counts.has_client_data() else "—",
            "note": "открытий приложения",
            "spark": charts.sparkline(daily.sessions),
            "client": True,
        },
        {
            "label": "Вернулись через неделю",
            "value": pct_text(back, came),
            "note": f"из {came} новых, чьи 2-я неделя закрылась" if came else "когорт периода ещё нет",
            "spark": charts.sparkline(week_spark),
        },
        {
            "label": "Нажали «Пойду»",
            "value": pct_text(intents.devices_window, active_w),
            "note": f"{intents.devices_window} из {active_w} активных, {window} дн.",
            "spark": charts.sparkline(share_spark(daily.intent_devices)),
        },
        {
            "label": "Записали выход",
            "value": pct_text(rec_w, active_w) if counts.has_client_data() else "—",
            "note": f"{rec_w} из {active_w} активных, {window} дн.",
            "spark": charts.sparkline(share_spark(daily.rec_devices)),
            "client": True,
        },
    ]
    return {
        "tiles": tiles,
        "active_line": charts.line(
            [Series("Активные", daily.active), Series("Новые", daily.new, ACCENT)], days
        ),
    }


async def _launch(session: AsyncSession, counts: Counts, daily: Daily, cohorts: Cohorts,
                  days: list[date], today: date, rsince: date) -> dict:
    visits = counts.by_key("landing")
    clicks: dict[str, int] = defaultdict(int)
    for kind in ("store_ios", "store_android"):
        for mark, n in counts.by_key(kind).items():
            clicks[mark] += n
    marks = sorted(set(visits) | set(clicks), key=lambda m: (-visits.get(m, 0), m))
    channels = [
        {
            "mark": mark or "без метки",
            "visits": visits.get(mark, 0),
            "clicks": clicks.get(mark, 0),
            "conv": pct_text(clicks.get(mark, 0), visits.get(mark, 0)),
        }
        for mark in marks
    ]

    cols = [f"нед. {i}" for i in range(1, cohorts.max_index + 1)]
    grid = [
        [
            (pct(cohorts.returned[(w, i)], cohorts.size.get(w, 0)) or 0)
            if (w, i) in cohorts.returned
            else None
            for i in range(1, cohorts.max_index + 1)
        ]
        for w in cohorts.weeks
    ]
    cohort_rows = [f"{w.strftime('%d.%m')} · {cohorts.size.get(w, 0)}" for w in cohorts.weeks]

    d1, d7, fresh = await _returns(session, today, rsince)
    return {
        "new_line": charts.line([Series("Новые устройства", daily.new)], days),
        "channels": channels,
        "cohorts": charts.heatmap(cohort_rows, cols, grid, unit=" %") if cols else None,
        "cohort_weeks": len(cohorts.weeks),
        "d1": d1,
        "d7": d7,
        "fresh": fresh,
    }


async def _returns(session: AsyncSession, today: date, rsince: date) -> tuple[str, str, int]:
    """Возврат новых устройств: на следующий день и в течение недели.

    Только по тем, у кого окно уже закрылось: для D1 — пришли не позже
    вчера, для D7 — не позже недели назад. Иначе свежие устройства
    занижали бы долю просто потому, что их завтра ещё не наступило.
    """
    fresh = {
        device: first
        for device, first in (
            await session.execute(
                select(Device.device, Device.first_seen).where(
                    Device.first_seen > rsince, Device.first_seen < today
                )
            )
        ).all()
    }
    if not fresh:
        return "—", "—", 0
    seen: dict[str, set[date]] = defaultdict(set)
    for device, day in (
        await session.execute(
            select(ApiEvent.device, cast(ApiEvent.ts, Date))
            .where(ApiEvent.device.in_(list(fresh)))
            .group_by(ApiEvent.device, cast(ApiEvent.ts, Date))
        )
    ).all():
        seen[device].add(day)
    d1_pool = [d for d, first in fresh.items() if first <= today - timedelta(days=1)]
    d7_pool = [d for d, first in fresh.items() if first <= today - timedelta(days=7)]
    d1 = sum(1 for d in d1_pool if fresh[d] + timedelta(days=1) in seen[d])
    d7 = sum(
        1
        for d in d7_pool
        if any(fresh[d] + timedelta(days=k) in seen[d] for k in range(1, 8))
    )
    return pct_text(d1, len(d1_pool)), pct_text(d7, len(d7_pool)), len(fresh)


async def _places(session: AsyncSession, counts: Counts, unique: Unique, intents: Intents,
                  sort: str, since: date, today: date, window: int) -> dict:
    meta = {
        slug: {"name": name, "category": getattr(category, "value", category), "region": region}
        for slug, name, category, region in (
            await session.execute(
                select(Place.slug, Place.name, Place.category, Region.name).join(
                    Region, Region.id == Place.region_id
                )
            )
        ).all()
    }
    pace = {
        slug: (faster, expected, slower)
        for slug, faster, expected, slower in (
            await session.execute(
                select(
                    Place.slug, PlacePaceStats.faster, PlacePaceStats.expected, PlacePaceStats.slower
                ).join(PlacePaceStats, PlacePaceStats.place_id == Place.id)
            )
        ).all()
    }
    downloads = await _downloads(session, counts)

    opens = counts.by_key("place")
    nav = counts.by_key("nav_start")
    finish = counts.by_key("nav_finish")
    offtrail = counts.by_key("nav_offtrail")
    aborted = counts.by_key("nav_abort")
    rec = counts.by_key("rec_finish")
    slugs = set(opens) | set(nav) | set(finish) | set(rec) | set(intents.votes)
    slugs.discard("")

    rows = []
    for slug in slugs:
        n_nav, n_fin = nav.get(slug, 0), finish.get(slug, 0)
        rows.append(
            {
                "slug": slug,
                "name": meta.get(slug, {}).get("name", slug),
                "category": meta.get(slug, {}).get("category", "other"),
                "region": meta.get(slug, {}).get("region", "—"),
                "opens": opens.get(slug, 0),
                "devices": unique.by_place.get(slug, 0),
                "votes": intents.votes.get(slug, 0),
                "nav": n_nav,
                "finish": n_fin,
                "offtrail": offtrail.get(slug, 0),
                "abort": aborted.get(slug, 0),
                "rec": rec.get(slug, 0),
                "finish_share": (n_fin / n_nav) if n_nav else -1,
                "finish_share_text": pct_text(n_fin, n_nav),
                "pace": pace.get(slug),
                "pace_text": _pace_text(pace.get(slug)),
                "downloads": downloads.get(slug, 0),
            }
        )
    key = SORTS[sort]
    rows.sort(key=lambda r: (-r[key], -r["opens"], r["name"]))
    top = rows[:TOP_LIMIT]

    look = sorted(
        (r for r in rows if r["opens"] >= LOOK_MIN_OPENS and r["nav"] == 0),
        key=lambda r: -r["opens"],
    )[:10]
    track = []
    for r in sorted(rows, key=lambda r: -r["nav"]):
        if r["nav"] < TRACK_MIN_NAV:
            continue
        reasons = []
        if r["offtrail"] / r["nav"] > TRACK_OFFTRAIL_SHARE:
            reasons.append(f"сходов {pct_text(r['offtrail'], r['nav'])}")
        if r["finish"] / r["nav"] < TRACK_FINISH_SHARE:
            reasons.append(f"финишей {pct_text(r['finish'], r['nav'])}")
        if reasons:
            track.append({**r, "reason": ", ".join(reasons)})

    def grouped(field_name: str, names: dict[str, str] | None = None) -> list[tuple[str, int, int]]:
        acc: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in rows:
            label = r[field_name]
            label = names.get(label, label) if names else label
            acc[label][0] += r["opens"]
            acc[label][1] += r["nav"]
        return sorted(((k, v[0], v[1]) for k, v in acc.items()), key=lambda t: -t[1])[:12]

    legend = ("Открытия", "Навигации")
    search = sorted(counts.by_key("search").items(), key=lambda kv: -kv[1])[:20]
    filters = sorted(counts.by_key("filter").items(), key=lambda kv: -kv[1])[:20]
    return {
        "top": top,
        "look": look,
        "track": track,
        "categories": charts.paired(grouped("category", CATEGORY_RU), legend),
        "regions": charts.paired(grouped("region"), legend),
        "search": search,
        "filters": [(_filter_ru(k), n) for k, n in filters],
        "upcoming": intents.upcoming,
        "shares": await _shares(counts, meta),
        "has_nav": bool(nav),
        "window": window,
    }


def _pace_text(counts: tuple[int, int, int] | None) -> str:
    """Как прошли: быстрее · так · дольше; прочерк, пока никто не ответил."""
    if not counts or not any(counts):
        return "—"
    faster, expected, slower = counts
    return f"{faster} · {expected} · {slower}"


FILTER_RU = {
    "day": "однодневные",
    "kids": "с детьми",
    "season": "по сезону",
}


def _filter_ru(key: str) -> str:
    if key in FILTER_RU:
        return FILTER_RU[key]
    prefix, _, value = key.partition(":")
    names = {
        "cat": "категория",
        "diff": "сложность",
        "drive": "дорога до",
        "hike": "пешком",
        "region": "регион",
        "collection": "подборка",
    }
    if prefix == "cat":
        value = CATEGORY_RU.get(value, value)
    return f"{names.get(prefix, prefix)}: {value}"


async def _downloads(session: AsyncSession, counts: Counts) -> dict[str, int]:
    """Обращения за GPX по местам: событие знает только имя файла."""
    files = counts.by_key("gpx")
    if not files:
        return {}
    owners = dict(
        (
            await session.execute(
                select(PlaceTrack.gpx_file, Place.slug)
                .join(Place, Place.id == PlaceTrack.place_id)
                .where(PlaceTrack.gpx_file.in_(list(files)))
            )
        ).all()
    )
    out: dict[str, int] = defaultdict(int)
    for name, n in files.items():
        slug = owners.get(name)
        if slug:
            out[slug] += n
    return dict(out)


async def _shares(counts: Counts, meta: dict) -> list[dict]:
    """Работает ли «поделиться»: открытия /p/{slug} за период."""
    rows = sorted(counts.by_key("share").items(), key=lambda kv: -kv[1])[:20]
    return [
        {"slug": slug, "name": meta.get(slug, {}).get("name", slug), "opens": n}
        for slug, n in rows
        if slug
    ]


async def _trail(session: AsyncSession, counts: Counts, unique: Unique, intents: Intents,
                 rsince: date, retention: int) -> dict:
    steps = [
        ("Активные", unique.active),
        ("Открыли место", unique.get("place")),
        ("Нажали «Пойду»", intents.devices_retention),
        ("Запустили навигатор", unique.get("nav_start")),
        ("Дошли до финиша", unique.get("nav_finish")),
        ("Записали выход", unique.get("rec_finish")),
        ("Поделились наклейкой", unique.get("sticker_share")),
    ]
    rec_start, rec_finish = counts.total("rec_start"), counts.total("rec_finish")
    recording = {
        "started": rec_start,
        "finished": rec_finish,
        "finish_share": pct_text(rec_finish, rec_start),
        "pauses": _per(counts.total("rec_pause"), rec_finish),
        "autoresumes": _per(counts.total("rec_autoresume"), rec_finish),
        "merges": counts.get("rec_merge"),
        "renames": counts.get("rec_rename"),
        "deletes": counts.get("rec_delete"),
        "shared_files": counts.get("rec_share_file"),
    }
    sticker_open = counts.total("sticker_open")
    sticker = {
        "opened": sticker_open,
        "saved": counts.total("sticker_save"),
        "shared": counts.total("sticker_share"),
        "saved_share": pct_text(counts.total("sticker_save"), sticker_open),
        "shared_share": pct_text(counts.total("sticker_share"), sticker_open),
        "layouts": charts.bars(
            sorted(((f"раскладка {k}", n) for k, n in counts.by_key("sticker_layout").items()),
                   key=lambda t: t[0])
        ) if counts.by_key("sticker_layout") else None,
    }
    files = []
    for ext in ("gpx", "kml", "kmz"):
        opened, saved = counts.get("file_open", ext), counts.get("file_save", ext)
        files.append({"ext": ext.upper(), "opened": opened, "saved": saved,
                      "share": pct_text(saved, opened)})

    weekday_counts, hour_grid = await _rhythm(session, rsince)
    return {
        "funnel": charts.funnel(steps),
        "steps": steps,
        "recording": recording,
        "sticker": sticker,
        "files": files,
        "weekdays": charts.bars(list(zip(WEEKDAYS, weekday_counts)), horizontal=False),
        "hours": charts.heatmap(
            list(WEEKDAYS), [f"{h:02d}" for h in range(24)], hour_grid, show_values=False
        ),
        "retention": retention,
        "has_nav": unique.get("nav_start") > 0,
        "has_rec": rec_start > 0,
        "has_sticker": sticker_open > 0,
        "has_files": any(f["opened"] for f in files),
        "has_sessions": any(any(row) for row in hour_grid),
    }


def _per(total: int, finished: int) -> str:
    if not finished:
        return "—"
    return f"{total / finished:.1f}".replace(".", ",")


async def _rhythm(session: AsyncSession, rsince: date) -> tuple[list[int], list[list[int]]]:
    """Когда ходят и когда планируют: дни недели навигаций, день × час сессий.

    Время — ташкентское: сырьё лежит в UTC, а «утро субботы» у людей своё.
    """
    local = func.timezone(TZ, ApiEvent.ts)
    dow = cast(extract("dow", local), Integer)
    hour = cast(extract("hour", local), Integer)
    weekdays = [0] * 7
    grid = [[0] * 24 for _ in range(7)]
    for kind, d, h, n in (
        await session.execute(
            select(ApiEvent.kind, dow, hour, func.count())
            .where(
                cast(ApiEvent.ts, Date) > rsince,
                ApiEvent.kind.in_(("nav_start", "app_open")),
            )
            .group_by(ApiEvent.kind, dow, hour)
        )
    ).all():
        row = (int(d) + 6) % 7  # Postgres: 0 — воскресенье; у нас неделя с понедельника
        if kind == "nav_start":
            weekdays[row] += int(n)
        else:
            grid[row][int(h)] += int(n)
    return weekdays, grid


async def _app(session: AsyncSession, counts: Counts, daily: Daily, days: list[date],
               wsince: date, today: date, window: int) -> dict:
    colors = {"ios": GREEN, "android": ACCENT, "unknown": MUTED}
    order = ["ios", "android", "unknown"]
    stack = charts.stacked(
        [
            Series(PLATFORM_RU[p], daily.platforms[p], colors[p])
            for p in order
            if p in daily.platforms
        ],
        days,
    )

    # Устройства окна с их последним заголовком; без заголовка — августовская 1.0
    known = func.coalesce(Device.platform, literal_column("'unknown'"))
    rows = (
        await session.execute(
            select(
                known,
                Device.app_version,
                Device.lang,
                Device.os_major,
                func.count(distinct(ApiEvent.device)),
            )
            .select_from(ApiEvent)
            .outerjoin(Device, Device.device == ApiEvent.device)
            .where(cast(ApiEvent.ts, Date) > wsince, ApiEvent.device.is_not(None))
            .group_by(known, Device.app_version, Device.lang, Device.os_major)
        )
    ).all()
    minimums = {
        platform: parse_version(version)
        for platform, version in (
            await session.execute(select(AppUpdate.platform, AppUpdate.min_version))
        ).all()
    }
    by_platform: dict[str, int] = defaultdict(int)
    versions: dict[tuple[str, str], int] = defaultdict(int)
    langs: dict[str, int] = defaultdict(int)
    oses: dict[tuple[str, str], int] = defaultdict(int)
    for platform, version, lang, os_major, n in rows:
        n = int(n)
        by_platform[platform] += n
        versions[(platform, version or "—")] += n
        if lang:
            langs[lang] += n
        if os_major:
            oses[(platform, os_major)] += n
    total = sum(by_platform.values())
    platform_share = [
        {"platform": PLATFORM_RU.get(p, p), "devices": by_platform[p], "share": pct_text(by_platform[p], total)}
        for p in order
        if by_platform.get(p)
    ]
    version_rows = []
    for (platform, version), n in sorted(
        versions.items(), key=lambda kv: (order.index(kv[0][0]) if kv[0][0] in order else 9, -kv[1])
    ):
        below = (
            version != "—"
            and platform in minimums
            and parse_version(version) < minimums[platform]
        )
        version_rows.append(
            {
                "platform": PLATFORM_RU.get(platform, platform),
                "version": version if version != "—" else "без заголовка",
                "devices": n,
                "share": pct_text(n, by_platform[platform]),
                "below": below,
            }
        )
    lang_rows = [
        {"lang": lang, "devices": n, "share": pct_text(n, sum(langs.values()))}
        for lang, n in sorted(langs.items(), key=lambda kv: -kv[1])
    ]
    os_rows = [
        {"platform": PLATFORM_RU.get(p, p), "os": os_major, "devices": n}
        for (p, os_major), n in sorted(oses.items(), key=lambda kv: (kv[0][0], -kv[1]))
    ][:12]

    onboarding = counts.by_key("onboarding")
    done = onboarding.get("done", 0)
    skips = sorted(((k.split(":")[1], n) for k, n in onboarding.items() if k.startswith("skip:")),
                   key=lambda t: int(t[0]))
    skipped = sum(n for _, n in skips)
    permissions = []
    for key, label in (("notif", "Уведомления"), ("geo", "Геопозиция")):
        yes, no = counts.get("permission", f"{key}:yes"), counts.get("permission", f"{key}:no")
        permissions.append({"label": label, "yes": yes, "no": no, "share": pct_text(yes, yes + no)})

    offline_devices = (
        await session.execute(
            select(func.count(distinct(ApiEvent.device))).where(
                cast(ApiEvent.ts, Date) > wsince, ApiEvent.kind == "offline_use"
            )
        )
    ).scalar_one()

    pushes = []
    opened = counts.by_key("push_open")
    for a in (
        await session.execute(
            select(Announcement)
            .where(
                Announcement.status.in_(
                    (
                        AnnouncementStatus.sent.value,
                        AnnouncementStatus.failed.value,
                        AnnouncementStatus.sending.value,
                    )
                )
            )
            .order_by(Announcement.send_at.desc())
            .limit(20)
        )
    ).scalars():
        n_open = opened.get(str(a.id), 0)
        pushes.append(
            {
                "id": a.id,
                "title": a.title,
                "sent_at": a.send_at,
                "sent": a.sent_count,
                "failed": a.failed_count,
                "opened": n_open,
                "share": pct_text(n_open, a.sent_count),
                "status": a.status,
            }
        )

    return {
        "platforms": stack,
        "platform_share": platform_share,
        "versions": version_rows,
        "langs": lang_rows,
        "os": os_rows,
        "onboarding": {"done": done, "skipped": skipped, "skips": skips,
                       "done_share": pct_text(done, done + skipped)},
        "permissions": permissions,
        "offline": {"devices": int(offline_devices), "opens": counts.total("offline_use")},
        "pushes": pushes,
        "reminders": {"weekly": counts.get("reminder_open", "weekly"),
                      "eve": counts.get("reminder_open", "eve")},
        "has_header": any(p != "unknown" for p in by_platform),
        "has_onboarding": bool(onboarding),
        "has_permissions": any(p["yes"] or p["no"] for p in permissions),
        "has_push_opens": bool(opened),
        "window": window,
    }
