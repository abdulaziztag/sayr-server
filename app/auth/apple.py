"""Вход с Apple: проверка identity token, обмен кода, отзыв при удалении.

App Store (правило 4.8) требует рядом со входом через Telegram равноценный
вход с Apple. Приложение присылает identity token (JWT от Apple),
authorization code и — только при самом первом входе — имя.

- Токен проверяем ключами https://appleid.apple.com/auth/keys: издатель
  https://appleid.apple.com, получатель — bundle id приложения, срок
  и `nonce`. Nonce обязателен: приложение придумывает случайную строку,
  отдаёт Apple её SHA-256 (hex) — он и оказывается в токене, — а нам
  присылает саму строку. Хеш в токене виден любому, строка — только
  приложению, поэтому утёкший токен без неё не открывает аккаунт.
- Код меняем на refresh-токен Apple (/auth/token). Он нужен ровно для
  одного: при удалении аккаунта отозвать доступ Sayr к Apple ID (/auth/revoke) —
  тоже требование App Store. Клиентский секрет для обоих вызовов — JWT ES256,
  подписанный ключом Sign in with Apple (.p8): kid — Key ID, iss — Team ID,
  sub — bundle id, aud — https://appleid.apple.com.

Без ключа вход работает, а обмен и отзыв пропускаются — это видно в журнале.
"""

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import AppleRevoke, User
from .jwks import Jwks, ProviderUnavailable, TokenInvalid, verify
from .sealed import unseal

log = logging.getLogger(__name__)

ISSUER = "https://appleid.apple.com"
JWKS_URL = "https://appleid.apple.com/auth/keys"
TOKEN_URL = "https://appleid.apple.com/auth/token"
REVOKE_URL = "https://appleid.apple.com/auth/revoke"
ALGORITHMS = frozenset({"RS256"})
#: Apple разрешает секрету жить до полугода; нам хватает часа — он
#: собирается на каждый обмен и отзыв, которых единицы в день
SECRET_TTL_SEC = 3600

#: Отзыв после удаления: повтор не чаще раза в час (цикл ротации) и с растущей
#: паузой. Дольше месяца токен удалённого человека не держим даже ради
#: отзыва — /privacy обещает стереть всё за 30 дней
REVOKE_GIVE_UP_AFTER = timedelta(days=30)


@dataclass(frozen=True)
class AppleUser:
    sub: str


class RevokeFailed(Exception):
    pass


class AppleSignIn:
    def __init__(
        self,
        bundle_id: str,
        team_id: str = "",
        key_id: str = "",
        key: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.bundle_id = bundle_id
        self.team_id = team_id
        self.key_id = key_id
        self._key = key
        self._http = http or httpx.AsyncClient(timeout=10)
        self.jwks = Jwks(JWKS_URL, self._http)

    @property
    def can_exchange(self) -> bool:
        """Есть чем подписать клиентский секрет — обмен и отзыв возможны"""
        return bool(self.team_id and self.key_id and self._key)

    def client_secret(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.team_id,
                "iat": now,
                "exp": now + SECRET_TTL_SEC,
                "aud": ISSUER,
                "sub": self.bundle_id,
            },
            self._key,
            algorithm="ES256",
            headers={"kid": self.key_id},
        )

    async def verify(self, identity_token: str, nonce: str) -> AppleUser:
        """`nonce` — исходная строка приложения, не хеш. Сам хеш из токена
        не годится: он лежит в токене открыто, и принять его значило бы
        пустить любого, у кого есть только токен"""
        claims = await verify(
            identity_token,
            self.jwks,
            issuer=ISSUER,
            audience=self.bundle_id,
            algorithms=ALGORITHMS,
        )
        claim = claims.get("nonce")
        hashed = hashlib.sha256(nonce.encode()).hexdigest()
        if not nonce or not isinstance(claim, str) or not hmac.compare_digest(claim, hashed):
            raise TokenInvalid("nonce не совпал")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not 0 < len(sub) <= 128:
            raise TokenInvalid("sub не годится")
        return AppleUser(sub=sub)

    async def _post(self, url: str, data: dict) -> httpx.Response:
        try:
            return await self._http.post(
                url,
                data={"client_id": self.bundle_id, "client_secret": self.client_secret(), **data},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{url}: {type(exc).__name__}") from exc

    async def exchange(self, code: str) -> str | None:
        """Код → refresh-токен. None — обменять не вышло: вход от этого
        не ломается, только отзывать при удалении будет нечем"""
        if not code or not self.can_exchange:
            return None
        try:
            response = await self._post(
                TOKEN_URL, {"code": code, "grant_type": "authorization_code"}
            )
        except ProviderUnavailable as exc:
            log.warning("Apple: код не обменялся — %s", exc)
            return None
        try:
            body = response.json()
        except ValueError:
            body = {}
        token = body.get("refresh_token") if response.status_code == 200 else None
        if not isinstance(token, str) or not token:
            log.warning("Apple: код не обменялся (%s %s)", response.status_code, body.get("error"))
            return None
        return token

    async def revoke(self, refresh_token: str) -> None:
        """Отозвать доступ. Apple отвечает 200 и на уже отозванный токен"""
        if not self.can_exchange:
            raise RevokeFailed("нет ключа Sign in with Apple")
        response = await self._post(
            REVOKE_URL, {"token": refresh_token, "token_type_hint": "refresh_token"}
        )
        if response.status_code != 200:
            raise RevokeFailed(f"{response.status_code} {response.text[:200]}")


_apple: AppleSignIn | None = None


def get_apple() -> AppleSignIn | None:
    """Вход с Apple. None — нет bundle id, вход выключен.

    Отдельной функцией, а не объектом на импорте: тесты подставляют свой,
    с тестовыми ключами и без сети. Ключ .p8 читается один раз"""
    global _apple
    if not settings.apple_bundle_id:
        return None
    if _apple is None:
        key = None
        if settings.apple_key_path:
            try:
                key = Path(settings.apple_key_path).read_text("utf-8")
            except OSError as exc:
                log.warning("Apple: ключ %s не читается (%s)", settings.apple_key_path, exc)
        _apple = AppleSignIn(
            settings.apple_bundle_id, settings.apple_team_id, settings.apple_key_id, key
        )
    return _apple


# MARK: - Отзыв при удалении аккаунта


def queue_revoke(session: AsyncSession, user: User) -> None:
    """Поставить отзыв в той же транзакции, что и удаление: строка человека
    уйдёт, а шифр токена останется ждать, пока Apple не ответит"""
    if user.apple_refresh:
        session.add(AppleRevoke(token=user.apple_refresh))
    elif user.apple_sub:
        # Код тогда не обменялся (не было ключа или Apple не ответил) —
        # отзывать нечем. Видно в журнале, чтобы не думать, что отозвали
        log.warning("Apple: у удалённого аккаунта нет токена для отзыва")


def _backoff(attempts: int) -> timedelta:
    return timedelta(hours=min(2 ** max(0, attempts - 1), 24))


async def revoke_due(
    session: AsyncSession, apple: AppleSignIn | None = None, only: str | None = None
) -> int:
    """Отозвать созревшие. `only` — шифр одного токена: сразу после удаления
    пробуем только его, не заставляя человека ждать чужие повторы.
    Ошибки не поднимает — пишет в строку и в журнал. Возвращает, сколько
    отозвано. Коммитит сама"""
    apple = apple or get_apple()
    now = datetime.now(timezone.utc)
    # Два воркера крутят один цикл: занятую другим строку пропускаем
    query = (
        select(AppleRevoke)
        .where(AppleRevoke.run_after <= now)
        .order_by(AppleRevoke.id)
        .limit(20)
        .with_for_update(skip_locked=True)
    )
    if only is not None:
        query = query.where(AppleRevoke.token == only)
    rows = (await session.execute(query)).scalars().all()
    done = 0
    for row in rows:
        token = unseal(row.token)
        if token is None:
            log.warning("Apple: токен для отзыва не расшифровался (сменили SECRET_KEY?)")
            await session.delete(row)
            continue
        if apple is None or not apple.can_exchange:
            # Не вина Apple — попытку не тратим, ждём ключа
            row.last_error = "нет ключа Sign in with Apple"
            row.run_after = now + timedelta(hours=1)
            continue
        try:
            await apple.revoke(token)
        except (RevokeFailed, ProviderUnavailable) as exc:
            row.attempts += 1
            row.last_error = str(exc)[:500]
            row.run_after = now + _backoff(row.attempts)
            log.warning("Apple: отзыв не прошёл (попытка %s): %s", row.attempts, exc)
            continue
        await session.delete(row)
        done += 1
    # Старше месяца не держим даже ради отзыва: обещали стереть всё за 30 дней
    old = await session.execute(
        delete(AppleRevoke)
        .where(AppleRevoke.created_at < now - REVOKE_GIVE_UP_AFTER)
        .returning(AppleRevoke.id)
    )
    if gone := len(old.all()):
        log.error("Apple: %s отзывов так и не прошли за 30 дней — токены стёрты", gone)
    await session.commit()
    return done
