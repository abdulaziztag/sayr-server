"""События с телефона: что человек делает внутри приложения.

    POST /api/v1/events
    {"events": [{"id": "<uuid>", "kind": "nav_finish", "key": "azadbash",
                 "at": "2026-09-20T07:41:03Z"}, …]}   → 204

Пачка до ста событий. Ответ всегда 204: клиенту нечего делать с отказом,
кроме как повторить, а повтор гасится случайным номером события. Битые
события отбрасываются поштучно, соседи по пачке живут.

`ALLOWED_KINDS` — граница приватности (спека 2026-09-19-analytics-design).
Каждый ключ — либо slug места из каталога, либо значение из короткого
перечня; координаты, расстояния, длительности и свободный текст сюда
не проходят по построению. Новый вид появляется только в этом списке
и только после двух вопросов: не несёт ли ключ координаты или текст
и покрыт ли он строкой в политике.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..models import ApiEvent
from ..stats import parse_app_header, touch_device

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["events"])

BATCH_MAX = 100
#: Событий на устройство в сутки. Защита от зациклившегося клиента,
#: а не от человека: живой человек столько не нажмёт
DAILY_CAP = 500

_slug = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_format = re.compile(r"^(gpx|kml|kmz)$")

#: вид → правило ключа. None — ключа нет.
ALLOWED_KINDS: dict[str, re.Pattern[str] | None] = {
    # Жизнь приложения
    "app_open": None,
    "onboarding": re.compile(r"^(done|skip:[1-9])$"),
    "permission": re.compile(r"^(notif|geo):(yes|no)$"),
    "update_gate": re.compile(r"^\d+(\.\d+){0,3}$"),
    "offline_use": _slug,
    # Каталог и место. Поиск — только запрос, нашедший хотя бы одно место:
    # фильтр работает по вхождению в названия, значит сюда доходят лишь
    # подстроки названий каталога. Апострофы — узбекские oʻ и gʻ
    "search": re.compile(r"^[a-zа-яёʻʼ'0-9][a-zа-яёʻʼ'0-9 -]{0,39}$"),
    "filter": re.compile(
        r"^(cat:[a-z_]{1,20}|diff:[a-z_]{1,20}|drive:(\d{1,4}|any)|hike:[a-z_]{1,20}"
        r"|day|kids|region:\d{1,6}|collection:[a-z0-9-]{1,64}|season)$"
    ),
    "tab_map": None,
    "map_pin": _slug,
    "favorite": _slug,
    "photo_full": _slug,
    "coords_copy": _slug,
    "route_external": _slug,
    "share": _slug,
    "plan_day": _slug,
    "weather": _slug,
    "date_pick": _slug,
    # Тропа
    "nav_start": _slug,
    "nav_offtrail": _slug,
    "nav_finish": _slug,
    "nav_abort": _slug,
    "rec_start": _slug,
    "rec_pause": _slug,
    "rec_autoresume": _slug,
    "rec_finish": _slug,
    "rec_merge": None,
    "rec_rename": None,
    "rec_delete": None,
    "rec_share_file": None,
    "sticker_open": _slug,
    "sticker_layout": re.compile(r"^[1-8]$"),
    "sticker_save": _slug,
    "sticker_share": _slug,
    "file_open": _format,
    "file_save": _format,
    "tab_own": re.compile(r"^(places|tracks)$"),
    # Каналы. store_ios / store_android здесь нет намеренно: их пишет
    # сервер с редиректа, а клиент подделать клик в магазин не должен
    "push_open": re.compile(r"^\d{1,9}$"),
    "reminder_open": re.compile(r"^(weekly|eve)$"),
}

#: Виды, у которых ключ может быть пустым: запись и наклейка по чужому
#: файлу не привязаны ни к какому месту каталога
OPTIONAL_KEY = frozenset(
    {"rec_start", "rec_pause", "rec_autoresume", "rec_finish",
     "sticker_open", "sticker_save", "sticker_share"}
)

_EVENT_ID = re.compile(r"^[A-Za-z0-9-]{8,36}$")


class EventIn(BaseModel):
    """Поля намеренно почти без ограничений: строгая проверка идёт поштучно
    в validate_key и clamp_at, чтобы одно кривое событие не роняло 422
    всю пачку. Пределы длины здесь — только от раздутого тела."""

    id: str = Field(max_length=64)
    kind: str = Field(max_length=64)
    key: str | None = Field(default=None, max_length=512)
    at: str = Field(max_length=64)


class EventsIn(BaseModel):
    # Лишнее сверх BATCH_MAX отрезается, а не отвергается: клиент, который
    # прислал сто первое событие, должен получить 204 и стереть очередь,
    # иначе он будет слать ту же пачку до бесконечности
    events: list[EventIn] = Field(max_length=10_000)


def validate_key(kind: str, key: str | None) -> tuple[bool, str | None]:
    """(годится ли, нормализованный ключ). Пустой ключ — None."""
    rule = ALLOWED_KINDS.get(kind, False)
    if rule is False:
        return False, None
    value = (key or "").strip()
    if kind == "search":
        value = value.lower()
    if not value:
        return (rule is None or kind in OPTIONAL_KEY), None
    if rule is None:
        return False, None
    return (rule.match(value) is not None), value


def clamp_at(raw: str, now: datetime, retention_days: int) -> datetime | None:
    """Момент с телефона в окно «сейчас − срок хранения ÷ сейчас».

    События едут из гор с опозданием, и день им нужен настоящий, но часы
    на телефоне врут: будущее ложится на сейчас, глубокое прошлое —
    на границу окна, чтобы ротация всё равно его свернула.
    """
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    floor = now - timedelta(days=retention_days)
    return min(max(at, floor), now)


@router.post("/events", status_code=204)
async def post_events(
    body: EventsIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    device = (request.headers.get("x-device-id") or "").strip()[:64]
    info = parse_app_header(request.headers.get("x-sayr-app"))
    if not device or (info is not None and info.debug and not settings.stats_count_debug):
        return Response(status_code=204)

    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    dropped = 0
    for event in body.events[:BATCH_MAX]:
        ok, key = validate_key(event.kind, event.key)
        at = clamp_at(event.at, now, settings.stats_retention_days)
        if not ok or at is None or not _EVENT_ID.match(event.id):
            dropped += 1
            continue
        rows.append(
            {"client_id": event.id, "kind": event.kind, "slug": key, "device": device, "ts": at}
        )
    if dropped:
        log.debug("события: отброшено %d из %d от %s", dropped, len(body.events), device)
    if not rows:
        return Response(status_code=204)

    used = (
        await session.execute(
            select(func.count()).where(
                ApiEvent.device == device, ApiEvent.ts >= now - timedelta(days=1)
            )
        )
    ).scalar_one()
    room = DAILY_CAP - used
    if room <= 0:
        # Пачка принята и молча не пишется: ошибка заставила бы клиент
        # повторять её вечно, а потолок — про защиту базы, не про отказ
        return Response(status_code=204)

    await session.execute(
        insert(ApiEvent)
        .values(rows[:room])
        .on_conflict_do_nothing(
            index_elements=[ApiEvent.client_id],
            index_where=ApiEvent.client_id.is_not(None),
        )
    )
    await touch_device(session, device, info, now.date())
    await session.commit()
    return Response(status_code=204)
