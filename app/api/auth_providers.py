"""Вход через Telegram и Apple и привязка Telegram к аккаунту.

Спека: docs/superpowers/specs/2026-09-30-telegram-apple-login-design.md.
Отвечают так же, как вход по номеру (`VerifyOut`: токен и человек), —
приложению всё равно, каким способом человек вошёл.

Поиск аккаунта при входе через Telegram: сначала по аккаунту Telegram,
потом по подтверждённому номеру из токена (и тогда аккаунту дописывается
Telegram), иначе новый аккаунт. Других склеек нет: два уже заведённых
аккаунта одного человека остаются разными — доказать, что это он, нечем.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi_storages import StorageFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.apple import AppleSignIn, get_apple
from ..auth.jwks import ProviderUnavailable, TokenInvalid
from ..auth.names import clean_name, split_name
from ..auth.schemas import UserOut
from ..auth.sealed import seal
from ..auth.telegram_login import TelegramLogin, TelegramUser, get_telegram_login
from ..auth.tokens import current_user
from ..db import get_session
from ..models import User, avatar_storage
from ..services.images import drop_avatar, off_loop, store_avatar
from .auth import VerifyOut, _device, open_session
from .rooms import remember_telegram

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
me_router = APIRouter(prefix="/api/v1/me", tags=["me"])


class TelegramIn(BaseModel):
    """ID-токен из библиотеки Telegram — или код с PKCE, если библиотека
    отдала код: тогда его меняет сервер, секрет бота на телефон не едет"""

    id_token: str | None = Field(default=None, max_length=8192)
    code: str | None = Field(default=None, max_length=1024)
    code_verifier: str | None = Field(default=None, max_length=256)
    redirect_uri: str | None = Field(default=None, max_length=512)


class AppleIn(BaseModel):
    identity_token: str = Field(min_length=1, max_length=8192)
    authorization_code: str | None = Field(default=None, max_length=1024)
    #: Apple отдаёт имя только при самом первом входе — дальше их нет
    first_name: str | None = Field(default=None, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    #: Исходный nonce, хеш которого приложение отдало Apple. Необязателен:
    #: пришёл — сверяем с токеном
    nonce: str | None = Field(default=None, max_length=256)


async def _who(body: TelegramIn, login: TelegramLogin | None) -> TelegramUser:
    if login is None:
        raise HTTPException(status_code=503, detail="telegram_login_off")
    try:
        token = body.id_token
        if not token:
            if not (body.code and body.code_verifier and body.redirect_uri):
                raise HTTPException(status_code=422, detail="id_token_or_code_required")
            if not login.can_exchange:
                # Без секрета бота код не обменять — вход по коду не настроен
                raise HTTPException(status_code=503, detail="telegram_login_off")
            token = await login.exchange(body.code, body.code_verifier, body.redirect_uri)
        return await login.verify(token)
    except TokenInvalid as exc:
        log.info("вход через Telegram: токен не принят (%s)", exc)
        raise HTTPException(status_code=401, detail="telegram_token_invalid") from None
    except ProviderUnavailable as exc:
        log.warning("вход через Telegram: Telegram не ответил (%s)", exc)
        raise HTTPException(status_code=502, detail="telegram_unavailable") from None


async def _one(session: AsyncSession, *where, lock: bool = False) -> User | None:
    query = select(User).where(*where).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def _telegram_account(session: AsyncSession, who: TelegramUser) -> tuple[User, bool]:
    """Аккаунт для этого Telegram и «свежий ли» он: только что заведён или
    Telegram к нему только что привязан по номеру — тогда анкету можно
    дополнить из Telegram"""
    user = await _one(session, User.telegram_id == who.id)
    if user is not None:
        return user, False
    phone = who.phone
    if phone:
        # Под замком: два входа разом не должны привязать номер дважды
        owner = await _one(session, User.phone == phone, lock=True)
        if owner is not None and owner.telegram_id is None:
            owner.telegram_id = who.id
            return owner, True
        if owner is not None:
            # Номер у аккаунта с другим Telegram: номер переехал на новый
            # аккаунт Telegram (старый удалён, номер отдали другому). Чужой
            # аккаунт по такому совпадению не открываем — заводим новый
            # без номера
            phone = None
    # Две попытки разом (двойное нажатие) — вторая не упадёт на уникальности,
    # а найдёт заведённый первой
    new_id = (
        await session.execute(
            insert(User)
            .values(telegram_id=who.id, phone=phone)
            .on_conflict_do_nothing()
            .returning(User.id)
        )
    ).scalar_one_or_none()
    if new_id is None:
        user = await _one(session, User.telegram_id == who.id)
        if user is None:
            # Уникальность споткнулась о номер, который завели параллельно
            new_id = (
                await session.execute(
                    insert(User).values(telegram_id=who.id).returning(User.id)
                )
            ).scalar_one()
        else:
            return user, False
    return await _one(session, User.id == new_id), True


async def _take_phone(session: AsyncSession, user: User, phone: str | None) -> None:
    """Номер из Telegram — аккаунту без номера, если номер ничей: тогда
    запасной вход по номеру попадёт в тот же аккаунт. Чей-то — не трогаем,
    аккаунты не склеиваем"""
    if not phone or user.phone:
        return
    if await _one(session, User.phone == phone) is None:
        user.phone = phone


def _prefill(user: User, who: TelegramUser) -> None:
    """Анкета из Telegram — только в пустую и ещё не тронутую человеком:
    стёртое им самим не возвращаем при следующем входе"""
    if who.username and not user.telegram_username:
        user.telegram_username = who.username
    if user.profile_filled_at is None and not user.first_name and not user.last_name:
        user.first_name, user.last_name = split_name(who.name)


def _wants_avatar(user: User) -> bool:
    return user.profile_filled_at is None and not user.avatar


async def _avatar_from_telegram(
    session: AsyncSession, login: TelegramLogin, user: User, url: str | None
) -> None:
    """Фото из Telegram — тем же путём, что загруженное руками: предел
    размера и пикселей, 512 px, без EXIF, в отдельном потоке. Не вышло —
    человек просто без фото, вход от этого не ломается. Зовётся после
    коммита: соединение с базой на время скачивания отдано пулу"""
    data = await login.picture(url)
    if not data:
        return
    try:
        name = await off_loop(store_avatar, data, user.id)
    except Exception as exc:  # noqa: BLE001 — любая неудача значит «без фото»
        log.info("фото из Telegram не встало: %s", type(exc).__name__)
        return
    # Пока качали, человек мог поставить своё с другого телефона. Имя файла —
    # от содержимого: совпало с его — это его файл, стирать нельзя
    await session.refresh(user)
    if user.avatar:
        if Path(user.avatar.name).name != name:
            drop_avatar(name)
        return
    user.avatar = StorageFile(name=name, storage=avatar_storage)
    await session.commit()


@router.post("/telegram", response_model=VerifyOut)
async def telegram_sign_in(
    body: TelegramIn,
    request: Request,
    login: TelegramLogin | None = Depends(get_telegram_login),
    session: AsyncSession = Depends(get_session),
) -> VerifyOut:
    who = await _who(body, login)
    device = _device(request)[0]
    for attempt in (1, 2):
        try:
            user, fresh = await _telegram_account(session, who)
            await _take_phone(session, user, who.phone)
            if fresh:
                _prefill(user, who)
                await remember_telegram(session, user.id, who.id)
            avatar = fresh and _wants_avatar(user)
            out = await open_session(session, request, user, device)
            break
        except IntegrityError:
            # Параллельный вход успел привязать тот же Telegram или номер:
            # второй проход найдёт уже записанное
            await session.rollback()
            if attempt == 2:
                raise
    if avatar:
        await _avatar_from_telegram(session, login, user, who.picture)
        out.user = UserOut.of(user)
    return out


@me_router.post("/telegram", response_model=UserOut)
async def link_telegram(
    body: TelegramIn,
    user: User = Depends(current_user),
    login: TelegramLogin | None = Depends(get_telegram_login),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    """«Привязать Telegram» в Профиле: тот же вход Telegram, но результат
    ложится к текущему аккаунту. Telegram уже у другого аккаунта — 409,
    без склейки"""
    who = await _who(body, login)
    owner = await _one(session, User.telegram_id == who.id)
    if owner is not None and owner.id != user.id:
        raise HTTPException(status_code=409, detail="telegram_taken")
    if user.telegram_id == who.id:
        return UserOut.of(user)
    old = user.telegram_id
    user.telegram_id = who.id
    await _take_phone(session, user, who.phone)
    _prefill(user, who)
    await remember_telegram(session, user.id, who.id, old)
    avatar = _wants_avatar(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="telegram_taken") from None
    if avatar:
        await _avatar_from_telegram(session, login, user, who.picture)
    return UserOut.of(user)


async def _apple_account(session: AsyncSession, sub: str) -> User:
    """Аккаунт этого Apple ID или новый. С другими не склеиваем: номера
    у Apple нет, а почта бывает скрытой — совпадать не по чему"""
    user = await _one(session, User.apple_sub == sub)
    if user is not None:
        return user
    # Двойное нажатие: вторая попытка найдёт аккаунт, заведённый первой
    await session.execute(
        insert(User).values(apple_sub=sub).on_conflict_do_nothing(index_elements=[User.apple_sub])
    )
    return await _one(session, User.apple_sub == sub)


@router.post("/apple", response_model=VerifyOut)
async def apple_sign_in(
    body: AppleIn,
    request: Request,
    apple: AppleSignIn | None = Depends(get_apple),
    session: AsyncSession = Depends(get_session),
) -> VerifyOut:
    if apple is None:
        raise HTTPException(status_code=503, detail="apple_login_off")
    try:
        who = await apple.verify(body.identity_token, body.nonce)
    except TokenInvalid as exc:
        log.info("вход с Apple: токен не принят (%s)", exc)
        raise HTTPException(status_code=401, detail="apple_token_invalid") from None
    except ProviderUnavailable as exc:
        log.warning("вход с Apple: Apple не ответил (%s)", exc)
        raise HTTPException(status_code=502, detail="apple_unavailable") from None
    # Обмен — до базы: сеть не держит соединение из пула. Не вышел — вход
    # всё равно состоится, отзывать при удалении будет нечем (видно в журнале)
    refresh = await apple.exchange(body.authorization_code or "")

    user = await _apple_account(session, who.sub)
    if user.profile_filled_at is None and not user.first_name and not user.last_name:
        # Имя Apple присылает один раз — берём по тому же правилу, что из Telegram
        first = clean_name(body.first_name) or ""
        user.first_name = first
        user.last_name = (clean_name(body.last_name) or "") if first else ""
    if refresh:
        user.apple_refresh = seal(refresh)
    return await open_session(session, request, user, _device(request)[0])
