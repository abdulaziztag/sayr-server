"""Планировщик: созревшие объявления → пуши всем живым установкам.

Отправители подставляются снаружи словарём «платформа → корутина», поэтому
тесты гоняют логику на подменах, а боевой прогон (send_due) собирает
настоящие из настроек. Платформа без отправителя — не падение: её
устройства идут в «не дошло», а причина — в last_error объявления.
"""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Announcement, AnnouncementStatus, PushToken
from . import SendResult

TASHKENT = ZoneInfo("Asia/Tashkent")
Transport = Callable[[str, str, str, str | None], Awaitable[SendResult]]

# Сколько запросов держим в воздухе разом: и APNs, и FCM спокойно берут больше,
# но нам важнее не упереться в лимиты соединений на маленьком VPS
CONCURRENCY = 32


def now_tashkent() -> datetime:
    """Настенное время Ташкента без зоны — в нём записано send_at."""
    return datetime.now(TASHKENT).replace(tzinfo=None)


async def due_announcements(session: AsyncSession, now: datetime) -> list[Announcement]:
    rows = await session.execute(
        select(Announcement)
        .where(
            Announcement.status == AnnouncementStatus.scheduled.value,
            Announcement.send_at <= now,
        )
        .order_by(Announcement.send_at)
    )
    return list(rows.scalars())


async def send_announcement(
    session: AsyncSession, announcement: Announcement, transports: dict[str, Transport]
) -> None:
    # Статус меняется до отправки: второй прогон поверх первого не должен
    # разослать то же самое ещё раз
    announcement.status = AnnouncementStatus.sending.value
    await session.commit()

    query = select(PushToken).where(PushToken.disabled_at.is_(None))
    if announcement.audience_lang:
        query = query.where(PushToken.lang == announcement.audience_lang)
    if announcement.audience_city:
        query = query.where(PushToken.city == announcement.audience_city)
    tokens = list((await session.execute(query)).scalars())

    sem = asyncio.Semaphore(CONCURRENCY)
    sent = failed = 0
    missing: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    dead: list[tuple[PushToken, str]] = []
    # Ключи платформы не в порядке — остальные её токены не мучаем
    broken: set[str] = set()

    async def one(t: PushToken) -> None:
        nonlocal sent, failed
        transport = transports.get(t.platform)
        if transport is None or t.platform in broken:
            missing[t.platform] += 1
            failed += 1
            return
        async with sem:
            if t.platform in broken:
                missing[t.platform] += 1
                failed += 1
                return
            result = await transport(t.token, announcement.title, announcement.body, announcement.place_slug)
        if result.ok:
            sent += 1
            return
        failed += 1
        if result.fatal:
            broken.add(t.platform)
        if result.invalid_token:
            dead.append((t, result.error or "invalid"))
        if result.error:
            errors[result.error] += 1

    await asyncio.gather(*(one(t) for t in tokens))

    now = datetime.now(timezone.utc)
    for token, why in dead:
        token.disabled_at = now
        token.disabled_reason = why[:64]

    notes = [f"{p}: не настроено или ключ не в порядке ({n} устр.)" for p, n in missing.items()]
    notes += [f"{e} ×{n}" for e, n in errors.most_common(3)]
    announcement.sent_count = sent
    announcement.failed_count = failed
    announcement.sent_at = now
    announcement.last_error = "; ".join(notes) or None
    # Пустой каталог устройств — не провал: отправлять было некому
    announcement.status = (
        AnnouncementStatus.sent.value if sent > 0 or not tokens else AnnouncementStatus.failed.value
    )
    await session.commit()


async def run_once(
    session: AsyncSession, transports: dict[str, Transport], now: datetime | None = None
) -> list[Announcement]:
    """Один тик планировщика: разослать всё созревшее к `now` (Ташкент)."""
    due = await due_announcements(session, now or now_tashkent())
    for announcement in due:
        await send_announcement(session, announcement, transports)
    return due
