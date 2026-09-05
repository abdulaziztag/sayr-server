"""Регистрация пуш-токенов приложений.

Токен — ключ: повторная регистрация обновляет язык, город, версию
и `last_seen` и снимает `disabled_at` — токен, который система выдала
заново, живой. `X-Device-Id` — тот же, что в статистике.
"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import PushToken

router = APIRouter(prefix="/api/v1/push", tags=["push"])


class DeviceIn(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    platform: Literal["ios", "android"]
    lang: Literal["ru", "uz"] = "ru"
    city: str | None = Field(default=None, max_length=32)
    app_version: str | None = Field(default=None, max_length=16)


@router.post("/devices", status_code=204)
async def register_device(
    body: DeviceIn,
    device_id: str | None = Header(None, alias="X-Device-Id"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    now = datetime.now(timezone.utc)
    fields = {
        "platform": body.platform,
        "device": (device_id or "").strip()[:64] or None,
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
    await session.execute(delete(PushToken).where(PushToken.token == token))
    await session.commit()
    return Response(status_code=204)
