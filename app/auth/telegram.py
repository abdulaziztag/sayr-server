"""Шлюз телеграма: доставка кода в переписку с официальным аккаунтом.

https://core.telegram.org/gateway/api — четыре метода, из них нужны два:
`sendVerificationMessage` и `checkVerificationStatus`. Код придумывает
шлюз (`code_length`), проверяет тоже он.

Платную проверку `checkSendAbility` заранее не зовём: она стоит столько же,
сколько отправка, а узнать, что телеграма у номера нет, можно и по ответу
на саму отправку.

Оплата — через Fragment; при недоставке в отведённое время шлюз
возвращает деньги сам.
"""

import logging

import httpx

from .gateway import (
    CODE_EXPIRED,
    CODE_INVALID,
    CODE_MAX_ATTEMPTS,
    CODE_UNKNOWN,
    CODE_VALID,
    SEND_FAILED,
    SEND_NO_TELEGRAM,
    SEND_OK,
    SendResult,
)

log = logging.getLogger(__name__)

API = "https://gatewayapi.telegram.org"

#: Перечня ошибок в документации шлюза нет, поэтому отличаем «нет телеграма»
#: по подстроке, а оригинал пишем в журнал: после первых живых отказов
#: список уточним. Ошибка, которую мы не узнали, показывается человеку
#: как общая неудача — это честнее, чем сказать «у вас нет телеграма»
#: тому, у кого он есть
_NO_TELEGRAM = ("PHONE_NUMBER_INVALID", "PHONE_NUMBER_NOT_FOUND", "USER_NOT_FOUND")

_KNOWN_CHECKS = {CODE_VALID, CODE_INVALID, CODE_MAX_ATTEMPTS, CODE_EXPIRED}


class TelegramGateway:
    name = "telegram"

    def __init__(self, token: str, sender: str = "") -> None:
        self._token = token
        self._sender = sender
        self._client = httpx.AsyncClient(timeout=10)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict) -> tuple[bool, dict, str | None]:
        try:
            response = await self._client.post(
                f"{API}/{method}",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("шлюз телеграма: %s не ответил (%s)", method, exc)
            return False, {}, "transport"
        if not body.get("ok"):
            error = str(body.get("error") or "unknown")
            log.warning("шлюз телеграма: %s отказал (%s)", method, error)
            return False, {}, error
        return True, body.get("result") or {}, None

    async def send(
        self,
        phone: str,
        *,
        ttl_sec: int,
        code_length: int,
        callback_url: str | None = None,
    ) -> SendResult:
        payload: dict = {
            "phone_number": phone,
            "code_length": code_length,
            "ttl": ttl_sec,
        }
        if self._sender:
            payload["sender_username"] = self._sender
        if callback_url:
            payload["callback_url"] = callback_url

        ok, result, error = await self._call("sendVerificationMessage", payload)
        if ok:
            return SendResult(status=SEND_OK, request_id=result.get("request_id"))
        if error and any(mark in error.upper() for mark in _NO_TELEGRAM):
            return SendResult(status=SEND_NO_TELEGRAM, error=error)
        return SendResult(status=SEND_FAILED, error=error)

    async def check(self, request_id: str, code: str) -> str:
        ok, result, _ = await self._call(
            "checkVerificationStatus", {"request_id": request_id, "code": code}
        )
        if not ok:
            return CODE_UNKNOWN
        status = ((result.get("verification_status") or {}).get("status") or "").lower()
        return status if status in _KNOWN_CHECKS else CODE_UNKNOWN
