"""Порог версии для приложений: нужно ли обновиться, прежде чем работать дальше.

    GET /api/v1/app/update?platform=ios&version=1.2.3
    → {"force": true, "min_version": "1.3.0", "store_url": "https://…"}

Приложение спрашивает на старте и при возврате из фона; без сети молчит
и не мешает — порог нужен, чтобы отсечь сломанные версии, а не чтобы
запирать людей в горах без связи.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..models import AppUpdate

router = APIRouter(prefix="/api/v1/app", tags=["app"])


class AppUpdateOut(BaseModel):
    force: bool
    min_version: str | None = None
    store_url: str | None = None


def parse_version(value: str) -> tuple[int, ...]:
    """«1.2.3» → (1, 2, 3); хвосты вроде «1.2.3-beta» и «(42)» отбрасываются.

    Сравнивать надо числами: строкой «1.10.0» оказалась бы старше «1.9.0».
    """
    parts: list[int] = []
    for chunk in value.strip().split(".")[:4]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


@router.get("/update", response_model=AppUpdateOut)
async def app_update(
    platform: Literal["ios", "android"],
    version: str = Query(..., max_length=32),
    session: AsyncSession = Depends(get_session),
) -> AppUpdateOut:
    store = settings.app_store_url if platform == "ios" else settings.play_store_url
    row = await session.get(AppUpdate, platform)
    if row is None:
        return AppUpdateOut(force=False, store_url=store or None)
    outdated = parse_version(version) < parse_version(row.min_version)
    return AppUpdateOut(
        force=bool(row.force and outdated), min_version=row.min_version, store_url=store or None
    )
