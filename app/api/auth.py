"""Вход по номеру телефона: запросить код, проверить код, выйти.

Пароля нет. Человек вводит номер, код приходит в телеграм (СМС появится,
когда у владельца будет юрлицо и согласованное имя отправителя), после
подтверждения устройство получает долгий токен.

Кода на сервере нет ни в базе, ни в журнале: его придумывает и проверяет
канал (`app/auth/gateway.py`). Мы храним только заявку — номер, канал,
время и число попыток.

Спека: docs/superpowers/specs/2026-09-23-account-login-design.md
"""

import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth.gateway import (
    CODE_EXPIRED,
    CODE_INVALID,
    CODE_MAX_ATTEMPTS,
    CODE_VALID,
    SEND_NO_TELEGRAM,
    SEND_OK,
    CodeChannel,
)
from ..auth.schemas import UserOut
from ..auth.telegram import TelegramGateway
from ..auth.tokens import current_session, new_token
from ..config import settings
from ..db import get_session
from ..models import LoginRequest, TripIntent, User, UserSession
from ..stats import parse_app_header

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

#: Запросов кода в час и в сутки на номер, в час на устройство и на адрес.
#: Каждая отправка платная, поэтому лимиты жёстче, чем у остальных форм.
#: Счётчики живут в памяти процесса, как у формы обращений и игры сезонов:
#: перезапуск их обнуляет, и это осознанно — они держат скрипт, а не человека
LIMITS = {
    "phone": (3, 3600),
    "phone_day": (10, 86_400),
    "device": (5, 3600),
    "ip": (20, 3600),
}
_SEEN: dict[str, list[float]] = {}

_PHONE = re.compile(r"^\+\d{8,15}$")
_CODE = re.compile(r"^\d{4,8}$")


def _too_often(bucket: str, key: str) -> bool:
    if not key:
        return False
    limit, window = LIMITS[bucket]
    now = time.monotonic()
    for stale in [k for k, v in _SEEN.items() if all(now - t > 86_400 for t in v)]:
        del _SEEN[stale]
    slot = f"{bucket}:{key}"
    fresh = [t for t in _SEEN.get(slot, []) if now - t < window]
    _SEEN[slot] = fresh
    if len(fresh) >= limit:
        return True
    fresh.append(now)
    return False


def _is_test_phone(phone: str) -> bool:
    """Номер проверяющего из магазина: заявка без канала, код из настроек"""
    return bool(
        settings.login_test_phone
        and settings.login_test_code
        and phone == normalize_phone(settings.login_test_phone)
    )


def normalize_phone(raw: str) -> str | None:
    """Номер к виду +998901234567.

    Разбирать номера по-настоящему (какой оператор, существует ли код)
    не пытаемся: это делает шлюз, и он же берёт за это деньги. Наша задача —
    привести к единому виду, чтобы один и тот же человек не завёл два
    аккаунта, написав номер по-разному.
    """
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        digits = "+" + digits if digits.startswith("998") else "+998" + digits.lstrip("0")
    return digits if _PHONE.match(digits) else None


_channel: TelegramGateway | None = None


def get_channel() -> CodeChannel | None:
    """Канал доставки. None — вход выключен (нет токена шлюза).

    Отдельной зависимостью, чтобы тесты подставляли поддельный канал
    и не ходили в сеть.
    """
    global _channel
    if not settings.tg_gateway_token:
        return None
    if _channel is None:
        _channel = TelegramGateway(settings.tg_gateway_token, settings.tg_gateway_sender)
    return _channel


class RequestIn(BaseModel):
    phone: str = Field(min_length=6, max_length=24)
    #: На будущее: когда появится СМС, приложение сможет попросить её явно
    channel: str = "telegram"


class RequestOut(BaseModel):
    request_id: str
    channel: str
    expires_at: datetime
    resend_after: int


class VerifyIn(BaseModel):
    request_id: str = Field(min_length=36, max_length=36)
    code: str = Field(min_length=4, max_length=8)


class VerifyOut(BaseModel):
    token: str
    user: UserOut


def _device(request: Request) -> tuple[str | None, str | None]:
    device = (request.headers.get("x-device-id") or "").strip()[:64] or None
    info = parse_app_header(request.headers.get("x-sayr-app"))
    return device, (info.platform if info else None)


@router.post("/request", response_model=RequestOut)
async def request_code(
    body: RequestIn,
    request: Request,
    channel: CodeChannel | None = Depends(get_channel),
    session: AsyncSession = Depends(get_session),
) -> RequestOut:
    if body.channel != "telegram":
        # СМС ждёт юрлица и согласования имени отправителя у операторов
        raise HTTPException(status_code=503, detail="channel_not_ready")

    phone = normalize_phone(body.phone)
    if phone is None:
        raise HTTPException(status_code=422, detail="phone_invalid")
    test_phone = _is_test_phone(phone)
    if channel is None and not test_phone:
        raise HTTPException(status_code=503, detail="channel_off")

    device, platform = _device(request)
    ip = request.client.host if request.client else ""
    if (
        _too_often("phone", phone)
        or _too_often("phone_day", phone)
        or _too_often("device", device or "")
        or _too_often("ip", ip)
    ):
        raise HTTPException(status_code=429, detail="too_often")

    now = datetime.now(timezone.utc)
    if test_phone:
        # Проверяющему App Store и Play код в телеграм не приходит: заявка
        # заводится без канала, а код сравнивается с настройкой при проверке
        row = LoginRequest(
            id=str(uuid.uuid4()),
            phone=phone,
            channel="test",
            expires_at=now + timedelta(seconds=settings.login_code_ttl_sec),
            device_id=device,
            ip=ip[:45] or None,
        )
        session.add(row)
        await session.commit()
        return RequestOut(
            request_id=row.id,
            channel="telegram",
            expires_at=row.expires_at,
            resend_after=settings.login_resend_after_sec,
        )

    assert channel is not None
    sent = await channel.send(
        phone,
        ttl_sec=settings.login_code_ttl_sec,
        code_length=settings.login_code_length,
        callback_url=settings.tg_gateway_callback_url or None,
    )
    if sent.status == SEND_NO_TELEGRAM:
        # Единственный отказ со своим экраном: человеку надо объяснить,
        # что дело не в нём и не в номере, а в том, что канал пока один
        raise HTTPException(status_code=409, detail="no_telegram")
    if sent.status != SEND_OK:
        log.warning("вход: канал %s не отправил код (%s)", channel.name, sent.error)
        raise HTTPException(status_code=502, detail="channel_failed")

    row = LoginRequest(
        id=str(uuid.uuid4()),
        phone=phone,
        channel=channel.name,
        gateway_request_id=sent.request_id,
        expires_at=now + timedelta(seconds=settings.login_code_ttl_sec),
        device_id=device,
        ip=ip[:45] or None,
    )
    session.add(row)
    await session.commit()
    return RequestOut(
        request_id=row.id,
        channel=channel.name,
        expires_at=row.expires_at,
        resend_after=settings.login_resend_after_sec,
    )


@router.post("/verify", response_model=VerifyOut)
async def verify_code(
    body: VerifyIn,
    request: Request,
    channel: CodeChannel | None = Depends(get_channel),
    session: AsyncSession = Depends(get_session),
) -> VerifyOut:
    if not _CODE.match(body.code):
        raise HTTPException(status_code=422, detail="code_invalid_format")

    row = await session.get(LoginRequest, body.request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="request_not_found")
    if channel is None and row.channel != "test":
        raise HTTPException(status_code=503, detail="channel_off")
    now = datetime.now(timezone.utc)
    if (
        row.status != "sent"
        or row.expires_at <= now
        or row.attempts >= settings.login_max_attempts
    ):
        if row.status == "sent":
            row.status = "expired"
            await session.commit()
        raise HTTPException(status_code=410, detail="code_expired")

    if row.channel == "test":
        # Настройка могла смениться после заявки: сравниваем с текущей
        status = (
            CODE_VALID
            if settings.login_test_code and body.code == settings.login_test_code
            else CODE_INVALID
        )
    else:
        assert channel is not None
        status = await channel.check(row.gateway_request_id or "", body.code)

    if status == CODE_VALID:
        row.status = "verified"
        user = (
            await session.execute(select(User).where(User.phone == row.phone))
        ).scalar_one_or_none()
        if user is None:
            user = User(phone=row.phone)
            session.add(user)
            await session.flush()
        user.last_login_at = now
        await _adopt_intents(session, user, row.device_id)
        token, digest = new_token()
        session.add(
            UserSession(
                user_id=user.id,
                token_hash=digest,
                device_id=row.device_id,
                platform=_device(request)[1],
            )
        )
        await session.commit()
        return VerifyOut(token=token, user=UserOut.of(user))

    if status == CODE_INVALID:
        row.attempts += 1
        left = max(0, settings.login_max_attempts - row.attempts)
        if left == 0:
            row.status = "expired"
        await session.commit()
        raise HTTPException(
            status_code=400 if left else 410,
            detail={"error": "code_invalid" if left else "code_expired", "attempts_left": left},
        )

    if status in (CODE_MAX_ATTEMPTS, CODE_EXPIRED):
        row.status = "expired"
        await session.commit()
        raise HTTPException(status_code=410, detail="code_expired")

    # Канал ответил незнакомым: попытку не сжигаем, человек не виноват
    log.warning("вход: канал ответил непонятным статусом для заявки %s", row.id)
    raise HTTPException(status_code=502, detail="channel_failed")


async def _adopt_intents(session: AsyncSession, user: User, device_id: str | None) -> None:
    """Планы этого устройства достаются человеку.

    Он отмечал «Пойду» гостем, а теперь вошёл — терять отметки нельзя.
    Сначала убираем гостевые, на которые у него уже есть свой голос
    с другого телефона: иначе один человек считался бы дважды.
    """
    if not device_id:
        return
    mine = aliased(TripIntent)
    already = (
        select(mine.id)
        .where(
            mine.user_id == user.id,
            mine.place_id == TripIntent.place_id,
            mine.day == TripIntent.day,
        )
        .exists()
    )
    await session.execute(
        delete(TripIntent).where(
            TripIntent.device_id == device_id, TripIntent.user_id.is_(None), already
        )
    )
    await session.execute(
        update(TripIntent)
        .where(TripIntent.device_id == device_id, TripIntent.user_id.is_(None))
        .values(user_id=user.id)
    )


@router.post("/logout", status_code=204)
async def logout(
    row: UserSession = Depends(current_session),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Гасит вход только на этом устройстве: остальные телефоны не трогаем."""
    row.revoked_at = datetime.now(timezone.utc)
    await session.commit()


class DeliveryIn(BaseModel):
    request_id: str
    delivery_status: dict | None = None


@router.post("/callback/telegram", status_code=204, include_in_schema=False)
async def delivery_report(
    body: DeliveryIn,
    key: str = "",
    session: AsyncSession = Depends(get_session),
) -> None:
    """Отчёт шлюза о доставке.

    Ручка ничего не открывает и никого не впускает: она умеет только
    закрыть заявку, которую шлюз не смог доставить. Ключ в адресе —
    чтобы чужой не гасил чужие заявки; пустой ключ в настройках выключает
    ручку совсем.
    """
    if not settings.tg_gateway_callback_key or key != settings.tg_gateway_callback_key:
        raise HTTPException(status_code=404, detail="not_found")
    status = ((body.delivery_status or {}).get("status") or "").lower()
    if status not in ("expired", "revoked"):
        return
    row = (
        await session.execute(
            select(LoginRequest).where(
                LoginRequest.gateway_request_id == body.request_id,
                LoginRequest.status == "sent",
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        row.status = "failed"
        await session.commit()
