"""Профиль: анкета, фото, удаление аккаунта.

Анкету видят попутчики (спека попутчиков), номер телефона им не уезжает
никогда. Здесь же обязательное по правилам App Store удаление аккаунта
изнутри приложения.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi_storages import StorageFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.schemas import UserOut
from ..auth.tokens import current_user
from ..db import get_session
from ..models import Gender, LoginRequest, User, avatar_storage
from ..moderation import is_clean
from ..services.images import drop_avatar, store_avatar
from .rooms import on_account_deleted

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/me", tags=["me"])

#: Столько лет человеку в горах уже можно; верх — чтобы отсечь опечатки
#: вроде 1080 года рождения, а не чтобы кого-то не пустить
MIN_BIRTH_YEAR = 1930


class ProfileIn(BaseModel):
    """Всё необязательное: анкета пропускается, поля правятся по одному.

    Чего нет в теле, того не трогаем; поле с `null` — стираем. Так приложение
    снимает год рождения и пол, которые человек передумал показывать."""

    first_name: str | None = Field(default=None, max_length=60)
    last_name: str | None = Field(default=None, max_length=60)
    gender: Gender | None = None
    birth_year: int | None = None
    telegram_username: str | None = Field(default=None, max_length=40)

    @field_validator("telegram_username")
    @classmethod
    def _nick(cls, value: str | None) -> str | None:
        if value is None:
            return None
        nick = value.strip().lstrip("@")
        if not nick:
            # Стёртое поле — «ника нет», а не пустая строка: иначе анкета
            # показывала бы при следующем открытии одинокое «@»
            return None
        if not nick.replace("_", "").isalnum() or len(nick) < 4:
            raise ValueError("ник в телеграме выглядит неправильно")
        return nick


@router.get("", response_model=UserOut)
async def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.of(user)


@router.patch("", response_model=UserOut)
async def update_me(
    body: ProfileIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    if body.birth_year is not None:
        year = datetime.now(timezone.utc).year
        if not MIN_BIRTH_YEAR <= body.birth_year <= year:
            raise HTTPException(status_code=422, detail="birth_year_invalid")
    # Имя видят попутчики: мат в нём — то же, что мат в заметке комнаты
    if not is_clean(body.first_name) or not is_clean(body.last_name):
        raise HTTPException(status_code=422, detail="bad_text")

    fields = body.model_dump(exclude_unset=True)
    for name, value in fields.items():
        setattr(user, name, value)
    if fields and user.profile_filled_at is None:
        user.profile_filled_at = datetime.now(timezone.utc)
    await session.commit()
    return UserOut.of(user)


@router.post("/avatar", response_model=UserOut)
async def set_avatar(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    data = await file.read()
    try:
        name = store_avatar(data, user.id)
    except ValueError:
        raise HTTPException(status_code=413, detail="file_too_big") from None
    except OSError:
        # Не картинка или формат, который Pillow не открывает (HEIC):
        # перекодировать в JPEG — дело приложения
        raise HTTPException(status_code=415, detail="not_an_image") from None

    old = Path(user.avatar.name).name if user.avatar else None
    user.avatar = StorageFile(name=name, storage=avatar_storage)
    if user.profile_filled_at is None:
        user.profile_filled_at = datetime.now(timezone.utc)
    await session.commit()
    if old and old != name:
        drop_avatar(old)
    return UserOut.of(user)


@router.delete("/avatar", response_model=UserOut)
async def clear_avatar(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    old = Path(user.avatar.name).name if user.avatar else None
    user.avatar = None
    await session.commit()
    if old:
        drop_avatar(old)
    return UserOut.of(user)


async def forget_account(session: AsyncSession, user: User) -> str | None:
    """Всё удаление аккаунта, кроме коммита и файла фото.

    Одно на два пути: кнопка в приложении (delete_me) и письменная просьба,
    которую владелец исполняет в админке (admin.UserAdmin). Пока админка
    просто стирала строку, у человека оставалось фото по открытой ссылке,
    сообщения в группах и комнаты, живые без организатора.

    Отдаёт имя файла фото: убирать его вызывающий должен после коммита —
    иначе откат транзакции оставил бы анкету со ссылкой в никуда.
    """
    avatar = Path(user.avatar.name).name if user.avatar else None
    # Походы человека отменяются, из чужих групп Telegram его уберут —
    # до удаления: строки участия уйдут каскадом вместе с ним
    await on_account_deleted(session, user)
    # Заявки на код держатся за номер, а не за человека, и каскадом
    # не уходят. Номер — единственное, что мы о нём знали; после удаления
    # его не должно оставаться нигде
    await session.execute(delete(LoginRequest).where(LoginRequest.phone == user.phone))
    await session.delete(user)
    return avatar


@router.delete("", status_code=204)
async def delete_me(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Удаление аккаунта изнутри приложения — требование App Store.

    Уходит всё, что связано с человеком: сессии, анкета, фото, избранное,
    история выходов и планы (всё по внешним ключам с CASCADE). Обезличенные
    события статистики не трогаем — номера телефона в них нет и связать
    их с человеком нечем.
    """
    avatar = await forget_account(session, user)
    await session.commit()
    if avatar:
        drop_avatar(avatar)
    log.info("аккаунт удалён по просьбе из приложения")
