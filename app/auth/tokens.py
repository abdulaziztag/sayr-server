"""Токен входа и зависимости FastAPI «кто это».

Токен выдаётся один раз на устройство и живёт, пока его не погасят выходом
или удалением аккаунта. Срока нет намеренно: приложение не банк, а просьба
входить заново раз в месяц превратила бы аккаунт в помеху.

В базе лежит отпечаток, а не сам токен, — как у пароля. Сравнение по
отпечатку заодно даёт индекс: искать по хешу быстрее, чем перебирать.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import User, UserSession

#: Реже раза в сутки отметку «видели» не обновляем: запись на каждый
#: запрос стоит дороже, чем точность этого поля
SEEN_EVERY = timedelta(days=1)


def new_token() -> tuple[str, str]:
    """Возвращает (токен для клиента, отпечаток для базы)."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


async def optional_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User | None:
    """Человек, если вошёл. Гостю отвечает None — ручка решает сама.

    Гостевой путь обязан работать без токена: каталог, планы и статистика
    открыты всем, аккаунт нужен только там, где без него никак.
    """
    token = bearer(request)
    if not token:
        return None
    row = (
        await session.execute(
            select(UserSession).where(
                UserSession.token_hash == token_hash(token),
                UserSession.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    now = datetime.now(timezone.utc)
    if row.last_seen_at is None or now - row.last_seen_at > SEEN_EVERY:
        row.last_seen_at = now
        await session.commit()
    return await session.get(User, row.user_id)


async def current_user(user: User | None = Depends(optional_user)) -> User:
    """Человек или 401. Клиент по 401 гасит токен и возвращается в гостя."""
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


async def current_session(
    request: Request, session: AsyncSession = Depends(get_session)
) -> UserSession:
    """Сессия этого устройства — нужна выходу, чтобы погасить одну её."""
    token = bearer(request)
    row = None
    if token:
        row = (
            await session.execute(
                select(UserSession).where(
                    UserSession.token_hash == token_hash(token),
                    UserSession.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return row
