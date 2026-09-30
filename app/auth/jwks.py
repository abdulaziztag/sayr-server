"""Открытые ключи поставщика входа (JWKS) и проверка подписанного им токена.

Общая часть входа через Telegram и Apple: оба подписывают ID-токен своим
ключом и раздают открытые ключи по адресу JWKS. Ключи кэшируем: ходить
за ними на каждый вход незачем, а поставщик меняет их редко. Незнакомый
`kid` — повод перечитать список (поставщик выкатил новый ключ), но не чаще
раза в минуту: иначе поток токенов с выдуманными `kid` превращал бы нас
в прокси запросов к Telegram и Apple.
"""

import asyncio
import logging
import time
from typing import Any

import httpx
import jwt

log = logging.getLogger(__name__)

#: Сколько верим списку ключей без перечитывания
KEYS_TTL_SEC = 6 * 3600
#: Не чаще этого перечитываем список ради незнакомого kid
REFETCH_EVERY_SEC = 60
#: Допуск по часам: у телефона и у нас они расходятся на секунды
LEEWAY_SEC = 60


class TokenInvalid(Exception):
    """Токен не прошёл проверку: подпись, издатель, получатель, срок."""


class ProviderUnavailable(Exception):
    """Поставщик не ответил: проверить токен сейчас нечем. Не вина человека."""


class Jwks:
    def __init__(self, url: str, http: httpx.AsyncClient) -> None:
        self.url = url
        self._http = http
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = float("-inf")
        self._lock = asyncio.Lock()

    async def _fetch(self) -> None:
        try:
            response = await self._http.get(self.url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable(f"{self.url}: {type(exc).__name__}") from exc
        keys: dict[str, jwt.PyJWK] = {}
        for raw in data.get("keys") or []:
            kid = raw.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK(raw)
            except jwt.PyJWTError:
                # Ключ алгоритма, который мы не умеем: пропускаем его одного,
                # а не весь список
                log.info("JWKS %s: ключ %s пропущен", self.url, kid)
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            known = self._keys.get(kid)
            if known is not None and age < KEYS_TTL_SEC:
                return known
            if known is None and age < REFETCH_EVERY_SEC:
                raise TokenInvalid("незнакомый kid")
            try:
                await self._fetch()
            except ProviderUnavailable:
                if known is not None:
                    # Старый список лучше никакого: ключи живут месяцами
                    return known
                raise
            fresh = self._keys.get(kid)
            if fresh is None:
                raise TokenInvalid("незнакомый kid")
            return fresh


async def verify(
    token: str,
    jwks: Jwks,
    *,
    issuer: str,
    audience: str,
    algorithms: frozenset[str],
) -> dict[str, Any]:
    """Проверенные утверждения токена или TokenInvalid.

    `aud` сверяем сами, строкой: у Telegram это ID бота, и число в токене
    вместо строки не должно стоить человеку входа. Подпись, `iss` и `exp`
    (с допуском по часам) — средствами PyJWT.
    """
    if not token or len(token) > 8192:
        raise TokenInvalid("пустой или слишком длинный")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise TokenInvalid("не JWT") from exc
    alg, kid = header.get("alg"), header.get("kid")
    if alg not in algorithms or not isinstance(kid, str):
        raise TokenInvalid(f"алгоритм {alg!r} или kid не годятся")
    key = await jwks.key(kid)
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=[alg],
            issuer=issuer,
            leeway=LEEWAY_SEC,
            options={"verify_aud": False, "require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenInvalid(type(exc).__name__) from exc
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if audience not in {str(a) for a in audiences}:
        raise TokenInvalid("чужой aud")
    return claims
