"""Регистрация пуш-токенов приложений.

Токен — ключ: повторная регистрация обновляет язык, город, версию
и `last_seen` и снимает `disabled_at` — токен, который система выдала
заново, живой. `X-Device-Id` — тот же, что в статистике.

Устройство токена решает, кому уйдут личные пуши о комнатах: очередь
(push/outbox.py) шлёт их на токены устройств, где человек вошёл последним.
Номер устройства выбирает клиент, поэтому привязку даёт только вход:
у вошедшего устройство берётся из его сессии, а устройство, где последним
вошёл и не вышел кто-то другой, не достаётся ни гостю, ни чужому аккаунту, —
иначе, зная чужой номер, любой получал бы чужие пуши своим токеном.
Рассылкам всем подряд устройство не нужно, у гостя они работают как раньше.

Устройство держит последний вход, а не первый. Выход без сети — обычное
дело в горах — гасит токен только в телефоне, а сессия на сервере живёт
вечно: правило «кто вошёл первым» навсегда оставило бы без личных пушей
того, кто вошёл на этом телефоне следом. Цена обратная: кто знает чужой
номер устройства и войдёт с ним в свой аккаунт, заглушит хозяину личные
пуши, но читать их не станет. Без этой цены токен надо привязывать
к сессии, а не к номеру устройства, — это колонка в push_tokens.
"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.tokens import live_session
from ..db import get_session
from ..models import PushToken, UserSession
from ..schemas import NO_NUL

router = APIRouter(prefix="/api/v1/push", tags=["push"])


class DeviceIn(BaseModel):
    token: str = Field(min_length=16, max_length=512, pattern=NO_NUL)
    platform: Literal["ios", "android"]
    lang: Literal["ru", "uz"] = "ru"
    city: str | None = Field(default=None, max_length=32, pattern=NO_NUL)
    app_version: str | None = Field(default=None, max_length=16, pattern=NO_NUL)


async def _last_login(session: AsyncSession, device: str) -> UserSession | None:
    """Последний вход на устройстве — живой или уже погашенный."""
    return (
        await session.execute(
            select(UserSession)
            .where(UserSession.device_id == device)
            .order_by(UserSession.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.post("/devices", status_code=204)
async def register_device(
    body: DeviceIn,
    request: Request,
    device_id: str | None = Header(None, alias="X-Device-Id"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    now = datetime.now(timezone.utc)
    login = await live_session(request, session)
    header = (device_id or "").strip()[:64] or None
    # У вошедшего устройство — из сессии, заголовок не в счёт: сессию
    # заводит вход по коду, а заголовок можно написать любой
    device = login.device_id if login and login.device_id else header
    # Устройство, где последним вошёл и не вышел кто-то другой, не достаётся
    # никому: токен живёт без него и получает только общие рассылки. Лучше
    # не доставить личный пуш, чем доставить его чужому
    if device:
        last = await _last_login(session, device)
        if (
            last is not None
            and last.revoked_at is None
            and (login is None or last.user_id != login.user_id)
        ):
            device = None
    fields = {
        "platform": body.platform,
        "device": device,
        "lang": body.lang,
        "city": body.city,
        "app_version": body.app_version,
        "last_seen": now,
    }
    stmt = insert(PushToken).values(token=body.token, **fields)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PushToken.token],
        set_={**fields, "disabled_at": None, "disabled_reason": None},
    )
    await session.execute(stmt)
    await session.commit()
    return Response(status_code=204)


@router.delete("/devices/{token}", status_code=204)
async def forget_device(token: str, session: AsyncSession = Depends(get_session)) -> Response:
    if "\x00" in token:
        raise HTTPException(422, "Кривой токен")
    await session.execute(delete(PushToken).where(PushToken.token == token))
    await session.commit()
    return Response(status_code=204)
