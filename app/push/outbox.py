"""Личные пуши одному человеку — о комнатах попутчиков.

Ставятся строкой в `push_outbox` прямо из ручек API, а отправляет их тик
`sayr-push` раз в минуту вместе с объявлениями (sender.run_once): у API
нет ключей APNs и FCM, и держать их в двух местах незачем.

Текст собирается на отправке, по языку каждого устройства человека:
в очереди лежат только вид и данные.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PushOutbox, PushToken, UserSession
from ..typography import uz_display

#: Не дошло за двое суток — уже неактуально: «вас взяли» через неделю
#: только путает
STALE_AFTER = timedelta(days=2)
#: Канал уведомлений Android и стопка iOS: попутчики отдельно от новостей
CHANNEL = "rooms"

_MONTHS = {
    "ru": [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ],
    "uz": [
        "yanvar", "fevral", "mart", "aprel", "may", "iyun",
        "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr",
    ],
}

#: вид → язык → (заголовок, текст). {name} — имя человека, {place} — место,
#: {date} — день похода
TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    "room_request": {
        "ru": ("{name} просится с вами", "{place}, {date}"),
        "uz": ("{name} siz bilan bormoqchi", "{place}, {date}"),
    },
    "room_joined": {
        "ru": ("{name} теперь в комнате", "{place}, {date}"),
        "uz": ("{name} endi xonada", "{place}, {date}"),
    },
    "room_approved": {
        "ru": ("Вас взяли в компанию", "{place}, {date}"),
        "uz": ("Sizni kompaniyaga olishdi", "{place}, {date}"),
    },
    "room_declined": {
        "ru": ("Заявку не приняли", "{place}, {date}"),
        "uz": ("Arizangiz qabul qilinmadi", "{place}, {date}"),
    },
    "room_cancelled": {
        "ru": ("Поход отменён", "{place}, {date}"),
        "uz": ("Sayohat bekor qilindi", "{place}, {date}"),
    },
    "room_removed": {
        "ru": ("Вас убрали из комнаты", "{place}, {date}"),
        "uz": ("Sizni xonadan chiqarishdi", "{place}, {date}"),
    },
    "group_ready": {
        "ru": ("Группа в Telegram готова", "{place}, {date} — вступайте"),
        "uz": ("Telegram guruhi tayyor", "{place}, {date} — qoʻshiling"),
    },
}


def day_text(day: date, lang: str) -> str:
    """25 сентября / 25-sentabr"""
    months = _MONTHS["uz" if lang == "uz" else "ru"]
    if lang == "uz":
        return f"{day.day}-{months[day.month - 1]}"
    return f"{day.day} {months[day.month - 1]}"


def render(kind: str, params: dict, lang: str) -> tuple[str, str]:
    texts = TEXTS[kind]
    title, body = texts.get(lang) or texts["ru"]
    place = params.get("place_uz") if lang == "uz" and params.get("place_uz") else params.get("place", "")
    fields = {
        "name": params.get("name", ""),
        "place": place,
        "date": day_text(date.fromisoformat(params["day"]), lang) if params.get("day") else "",
    }
    # Узбекские знаки — и в имени человека, и в названии места
    return uz_display(title.format(**fields)), uz_display(body.format(**fields))


def enqueue(
    session: AsyncSession, user_id: int, kind: str, params: dict, room_code: str | None
) -> None:
    """Поставить пуш в очередь. Коммитит вызывающий — вместе со своим делом"""
    assert kind in TEXTS, kind
    session.add(PushOutbox(user_id=user_id, kind=kind, params=params, room_code=room_code))


async def send_outbox(session: AsyncSession, transports: dict) -> int:
    """Разослать очередь по устройствам людей. Возвращает, сколько ушло.

    Устройство человека — то, где он вошёл (живая сессия), с живым
    push-токеном этого же устройства. Нет таких — пуш просто снимается:
    в приложении человек и так увидит всё в комнате.

    Токен при этом должен быть прислан после входа. Гость не может
    привязать токен к устройству, где кто-то вошёл (api/push.py), но
    до входа устройство свободно: токен, присланный под чужим номером
    заранее, дождался бы, пока хозяин номера войдёт, и получал бы его
    пуши. Прислан после входа — значит, прислан под этой сессией.
    Своё приложение присылает токен заново само: iOS — при каждом
    возврате на экран, Android — при каждом запуске.
    """
    now = datetime.now(timezone.utc)
    rows = list(
        (
            await session.execute(
                select(PushOutbox)
                .where(PushOutbox.sent_at.is_(None), PushOutbox.created_at > now - STALE_AFTER)
                .order_by(PushOutbox.id)
                .limit(500)
            )
        ).scalars()
    )
    sent = 0
    for row in rows:
        tokens = (
            await session.execute(
                select(PushToken)
                .join(UserSession, UserSession.device_id == PushToken.device)
                .where(
                    UserSession.user_id == row.user_id,
                    UserSession.revoked_at.is_(None),
                    PushToken.last_seen >= UserSession.created_at,
                    PushToken.disabled_at.is_(None),
                )
            )
        ).scalars()
        for token in {t.token: t for t in tokens}.values():
            transport = transports.get(token.platform)
            if transport is None:
                continue
            title, body = render(row.kind, row.params, token.lang)
            extra = {"room": row.room_code} if row.room_code else None
            result = await transport(
                token.token, title, body, None, None, extra=extra, channel=CHANNEL
            )
            if result.ok:
                sent += 1
            elif result.invalid_token:
                token.disabled_at = now
                token.disabled_reason = (result.error or "invalid")[:64]
        row.sent_at = now
    await session.commit()
    return sent
