"""Отправка в FCM HTTP v1 с сервисным ключом проекта Firebase.

OAuth-токен получаем сами: подписанный ключом JWT меняется на access_token
на час. Библиотека google-auth умеет то же, но тащит полдесятка пакетов
ради одного запроса.
"""

import json
import time
from pathlib import Path

import httpx
import jwt

from . import SendResult

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


class FcmSender:
    def __init__(self, service_account_path: Path):
        info = json.loads(Path(service_account_path).read_text("utf-8"))
        self._email = info["client_email"]
        self._key = info["private_key"]
        self._token_uri = info.get("token_uri", "https://oauth2.googleapis.com/token")
        self._project = info["project_id"]
        self._access: str | None = None
        self._access_until = 0.0
        self._client = httpx.AsyncClient(timeout=10)

    async def _access_token(self) -> str:
        if self._access and time.time() < self._access_until - 60:
            return self._access
        now = int(time.time())
        assertion = jwt.encode(
            {"iss": self._email, "scope": SCOPE, "aud": self._token_uri, "iat": now, "exp": now + 3600},
            self._key,
            algorithm="RS256",
        )
        r = await self._client.post(
            self._token_uri,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )
        r.raise_for_status()
        data = r.json()
        self._access = data["access_token"]
        self._access_until = now + int(data.get("expires_in", 3600))
        return self._access

    async def send(self, token: str, title: str, body: str, slug: str | None) -> SendResult:
        message: dict = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "android": {"priority": "high"},
            }
        }
        if slug:
            message["message"]["data"] = {"slug": slug}
        try:
            access = await self._access_token()
            r = await self._client.post(
                f"https://fcm.googleapis.com/v1/projects/{self._project}/messages:send",
                json=message,
                headers={"authorization": f"Bearer {access}"},
            )
        except httpx.HTTPStatusError as e:
            # Не дали access_token: ключ сервисного аккаунта не в порядке
            return SendResult(ok=False, fatal=True, error=f"fcm auth {e.response.status_code}")
        except httpx.HTTPError as e:
            return SendResult(ok=False, error=f"fcm: {type(e).__name__}")
        if r.status_code == 200:
            return SendResult(ok=True)
        code = ""
        try:
            err = r.json().get("error", {})
            code = next(
                (d.get("errorCode") for d in err.get("details", []) if d.get("errorCode")),
                err.get("status", ""),
            )
        except ValueError:
            pass
        # Мёртвым считаем только UNREGISTERED: 400 INVALID_ARGUMENT бывает
        # и от кривого сообщения, и гасить по нему весь Android нельзя
        return SendResult(
            ok=False,
            invalid_token=r.status_code == 404 or code == "UNREGISTERED",
            fatal=r.status_code in (401, 403),
            error=f"fcm {r.status_code} {code}".strip(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
