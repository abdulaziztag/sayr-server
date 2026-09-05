"""Отправка в APNs напрямую: HTTP/2 и JWT-ключ разработчика.

Без Firebase на iOS: одной зависимостью меньше в приложении, а Apple
отвечает по каждому токену сама. «BadDeviceToken» и «Unregistered»
значат, что установка умерла, и токен надо погасить.
"""

import time
from pathlib import Path

import httpx
import jwt

from . import SendResult

PRODUCTION = "https://api.push.apple.com"
SANDBOX = "https://api.sandbox.push.apple.com"
# Apple просит обновлять JWT не чаще раза в 20 минут и не реже раза в час
JWT_TTL_SEC = 50 * 60
DEAD_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken"}


class ApnsSender:
    def __init__(self, key_path: Path, key_id: str, team_id: str, topic: str, sandbox: bool):
        self._key = Path(key_path).read_text("utf-8")
        self._key_id = key_id
        self._team_id = team_id
        self._topic = topic
        self._jwt: str | None = None
        self._jwt_at = 0.0
        self._client = httpx.AsyncClient(
            http2=True, base_url=SANDBOX if sandbox else PRODUCTION, timeout=10
        )

    def _bearer(self) -> str:
        if self._jwt is None or time.time() - self._jwt_at > JWT_TTL_SEC:
            self._jwt = jwt.encode(
                {"iss": self._team_id, "iat": int(time.time())},
                self._key,
                algorithm="ES256",
                headers={"kid": self._key_id},
            )
            self._jwt_at = time.time()
        return self._jwt

    async def send(self, token: str, title: str, body: str, slug: str | None) -> SendResult:
        payload: dict = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
        if slug:
            # Тот же ключ, что у локальных напоминаний: делегат в приложении
            # уже умеет открывать место по нему
            payload["slug"] = slug
        headers = {
            "authorization": f"bearer {self._bearer()}",
            "apns-topic": self._topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": "0",
        }
        try:
            r = await self._client.post(f"/3/device/{token}", json=payload, headers=headers)
        except httpx.HTTPError as e:
            return SendResult(ok=False, error=f"apns: {type(e).__name__}")
        if r.status_code == 200:
            return SendResult(ok=True)
        reason = ""
        try:
            reason = str(r.json().get("reason", ""))
        except ValueError:
            pass
        return SendResult(
            ok=False,
            invalid_token=r.status_code == 410 or reason in DEAD_REASONS,
            fatal=r.status_code in (401, 403),
            error=f"apns {r.status_code} {reason}".strip(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
