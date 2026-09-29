"""Вход по номеру телефона: запросить код, проверить код, выйти.

Пароля нет. Человек вводит номер, код приходит в телеграм (СМС появится,
когда у владельца будет юрлицо и согласованное имя отправителя), после
подтверждения устройство получает долгий токен.

Кода на сервере нет ни в базе, ни в журнале: его придумывает и проверяет
канал (`app/auth/gateway.py`). Мы храним только заявку — номер, канал,
время и число попыток.

Спека: docs/superpowers/specs/2026-09-23-account-login-design.md
"""

import ipaddress
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.errors import LockNotAvailable
from pydantic import BaseModel, Field
from sqlalchemy import Select, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
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
from .intents import take_back_votes

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

#: Запросов кода в час и в сутки на номер, в час на устройство и на адрес.
#: Каждая отправка платная, поэтому лимиты жёстче, чем у остальных форм.
#: Считаются по заявкам в базе, а не в памяти процесса: воркеров два,
#: и счётчики в памяти каждого удваивали платный лимит, обнулялись выкатом
#: и перебирались целиком на каждый запрос — скрипт со случайными номерами
#: раздувал их так, что лимит сам клал API
LIMITS = {
    "phone": (3, 3600),
    "phone_day": (10, 86_400),
    "device": (5, 3600),
    "ip": (20, 3600),
}

#: Ключ замка pg_advisory_xact_lock, под которым проверяются лимиты
#: и пишется заявка. Число любое, лишь бы не совпало с другими замками
_LIMITS_LOCK = 0x4C4F4749  # «LOGI»
#: Сколько ждать этот замок. Под ним пара счётов по индексам и одна запись:
#: если очередь не сходит за секунды, база занята чем-то другим, а каждый
#: ожидающий держит соединение из пула — дольше ждать значит положить API
_LOCK_WAIT = "2s"

#: Платный код: ушёл человеку, а не проверяющему. Условие — слово в слово
#: как у частичного индекса ix_login_requests_codes_created_at и строкой,
#: а не параметрами: общий план закешированного запроса не знает значений
#: параметров, не может взять этот индекс и пересчитывал бы все заявки
#: за сутки, включая неотправленные, — а их скрипт плодит больше всего
_PAID = text("status <> 'unsent' AND channel <> 'test'")
#: Обращение к шлюзу — любая заявка, кроме заявок проверяющего
_GATEWAY = text("channel <> 'test'")

#: Когда журнал последний раз слышал о потолке. Отбитые потолком запросы
#: ничего не пишут и могут идти сплошным потоком — строки в час хватит
_cap_warned_at = float("-inf")

_PHONE = re.compile(r"^\+\d{8,15}$")
_CODE = re.compile(r"^\d{4,8}$")


def _net(host: str) -> str:
    """Адрес для лимита. IPv6 — сетью /64: столько провайдер выдаёт одному
    абоненту, адреса внутри неё меняются бесплатно, и лимит на адрес
    обходился бы перебором последних цифр"""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host[:45]
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((addr, 64), strict=False))
    return str(addr)


def _counted(window: int, *where) -> Select:
    since = func.now() - timedelta(seconds=window)
    return (
        select(func.count())
        .select_from(LoginRequest)
        .where(LoginRequest.created_at > since, *where)
    )


async def _count(session: AsyncSession, window: int, *where) -> int:
    return (await session.execute(_counted(window, *where))).scalar_one()


def _warn_cap(what: str, cap: int) -> None:
    global _cap_warned_at
    if time.monotonic() - _cap_warned_at >= 3600:
        _cap_warned_at = time.monotonic()
        log.warning("вход: исчерпан суточный потолок %s (%s)", what, cap)


async def _check_limits(
    session: AsyncSession, phone: str, device: str | None, ip: str, test_phone: bool
) -> None:
    """429, если исчерпан лимит; 503, если исчерпан суточный потолок.

    Отбитый запрос ничего не пишет: скрипт, упёршийся в лимит адреса или
    устройства, больше не сжигает чужому номеру его квоту. Адрес и устройство
    считают все попытки, дошедшие до шлюза, номер — только ушедшие коды:
    сбой шлюза не вина человека.

    Каждый счёт — отрезок индекса по своему ключу за своё окно, а заявок
    за сутки не больше потолка обращений к шлюзу: цена проверки не растёт,
    сколько бы скрипт ни прислал.
    """
    sent = LoginRequest.status != "unsent"
    for bucket, key, where in (
        ("ip", ip, LoginRequest.ip == ip),
        ("device", device, LoginRequest.device_id == device),
        ("phone", phone, (LoginRequest.phone == phone) & sent),
        ("phone_day", phone, (LoginRequest.phone == phone) & sent),
    ):
        limit, window = LIMITS[bucket]
        if key and await _count(session, window, where) >= limit:
            raise HTTPException(status_code=429, detail="too_often")

    # Потолки бережут счёт шлюза, код проверяющего бесплатный
    if test_phone:
        return
    for cap, where, what in (
        (settings.login_gateway_calls_per_day, _GATEWAY, "обращений к шлюзу"),
        (settings.login_codes_per_day, _PAID, "кодов"),
    ):
        if cap and await _count(session, 86_400, where) >= cap:
            _warn_cap(what, cap)
            raise HTTPException(status_code=503, detail="daily_cap")


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
    ip = _net(request.client.host) if request.client else ""
    # Сначала без замка: скрипт, упёршийся в лимит, отбивается парой
    # счётов по индексу и не встаёт в общую очередь
    await _check_limits(session, phone, device, ip, test_phone)
    # Потом ещё раз под замком — и сразу пишем заявку: иначе пачка
    # параллельных запросов прошла бы вся, пока ни одной заявки ещё нет.
    # Замок транзакционный, коммит ниже его отпускает — к шлюзу идём без него.
    # Не дождались — 503: приложение скажет «попробуйте позже», а не «через час»
    await session.execute(select(func.set_config("lock_timeout", _LOCK_WAIT, True)))
    try:
        await session.execute(select(func.pg_advisory_xact_lock(_LIMITS_LOCK, 0)))
    except OperationalError as exc:
        if not isinstance(exc.orig, LockNotAvailable):
            raise
        raise HTTPException(status_code=503, detail="busy") from None
    await _check_limits(session, phone, device, ip, test_phone)

    now = datetime.now(timezone.utc)
    row = LoginRequest(
        id=str(uuid.uuid4()),
        phone=phone,
        # Проверяющему App Store и Play код в телеграм не приходит: заявка
        # заводится без канала, а код сравнивается с настройкой при проверке
        channel="test" if test_phone else channel.name,
        status="sent" if test_phone else "sending",
        expires_at=now + timedelta(seconds=settings.login_code_ttl_sec),
        device_id=device,
        ip=ip or None,
    )
    session.add(row)
    await session.commit()
    if test_phone:
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
    if sent.status != SEND_OK:
        # Заявка остаётся для лимитов адреса и устройства, а номеру
        # в квоту не идёт: код до него не доехал
        row.status = "unsent"
        await session.commit()
    if sent.status == SEND_NO_TELEGRAM:
        # Единственный отказ со своим экраном: человеку надо объяснить,
        # что дело не в нём и не в номере, а в том, что канал пока один
        raise HTTPException(status_code=409, detail="no_telegram")
    if sent.status != SEND_OK:
        log.warning("вход: канал %s не отправил код (%s)", channel.name, sent.error)
        raise HTTPException(status_code=502, detail="channel_failed")

    row.status = "sent"
    row.gateway_request_id = sent.request_id
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

    # Строка под замком до конца проверки: иначе параллельные догадки
    # читали бы одно и то же число попыток и проверялись сверх лимита.
    # Занятую не ждём: замок держится, пока шлюз проверяет код (до 10 с),
    # и догадки к одной заявке стояли бы в очереди, каждая с соединением
    # из пула. Приложение второй раз, не дождавшись ответа, код не шлёт
    try:
        row = (
            await session.execute(
                select(LoginRequest)
                .where(LoginRequest.id == body.request_id)
                .with_for_update(nowait=True)
            )
        ).scalar_one_or_none()
    except OperationalError as exc:
        if not isinstance(exc.orig, LockNotAvailable):
            raise
        raise HTTPException(status_code=429, detail="too_often") from None
    if row is None:
        raise HTTPException(status_code=404, detail="request_not_found")
    if channel is None and row.channel != "test":
        raise HTTPException(status_code=503, detail="channel_off")
    now = datetime.now(timezone.utc)
    if (
        row.status != "sent"
        or row.expires_at <= now
        or row.attempts >= settings.login_max_attempts
        # Номер стёрт удалением аккаунта, пока код был в пути
        or not row.phone
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
            # Две заявки на новый номер, подтверждённые разом (двойное нажатие,
            # два телефона): вторая дождётся первой и найдёт её человека,
            # а не упадёт на уникальности номера
            await session.execute(
                insert(User)
                .values(phone=row.phone)
                .on_conflict_do_nothing(index_elements=[User.phone])
            )
            user = (
                await session.execute(select(User).where(User.phone == row.phone))
            ).scalar_one()
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
    с другого телефона: иначе один человек считался бы дважды. Убираем
    вместе с ответом «как сходили» — иначе голос темпа остался бы висеть
    без отметки, как при обычном её снятии.
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
    gone = await session.execute(
        delete(TripIntent)
        .where(TripIntent.device_id == device_id, TripIntent.user_id.is_(None), already)
        .returning(TripIntent.place_id, TripIntent.pace)
    )
    await take_back_votes(session, gone.all())
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
