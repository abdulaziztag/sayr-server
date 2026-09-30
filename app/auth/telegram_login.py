"""Вход через Telegram — официальный OpenID Connect.

https://core.telegram.org/widgets/login. Библиотеки Telegram для iOS
и Android сами проводят человека через окно Telegram «Войти в Sayr?»
и отдают приложению ID-токен — JWT, подписанный Telegram. Приложение
присылает его нам, мы проверяем подпись ключами
https://oauth.telegram.org/.well-known/jwks.json, издателя, получателя
(`aud` — ID нашего бота) и срок.

На случай, если какая-то сборка библиотеки отдаст не токен, а код
авторизации, умеем и обмен кода на /token: Basic-авторизация Client ID
и Secret, PKCE-верификатор — из приложения.

Из токена берём:
- `id` — аккаунт Telegram. Не `sub`: в примере документации это разные
  числа, а группы комнат (Sayr Admin) знают людей именно по `id`;
- `name`, `preferred_username`, `picture` — для анкеты нового аккаунта;
- `phone_number` — только при `phone_number_verified`: по нему вход через
  Telegram и вход по номеру попадают в один аккаунт.

Библиотеки Telegram nonce не принимают, а ID-токен живёт час. Чтобы утёкший
токен не открывал аккаунт весь этот час, принимаем только свежевыданные
(`iat` не старше MAX_TOKEN_AGE_SEC): приложение шлёт токен сразу после окна.
"""

import ipaddress
import logging
import re
from dataclasses import dataclass

import httpx

from ..config import settings
from ..services.images import MAX_AVATAR_BYTES
from .jwks import Jwks, ProviderUnavailable, TokenInvalid, verify

log = logging.getLogger(__name__)

ISSUER = "https://oauth.telegram.org"
JWKS_URL = "https://oauth.telegram.org/.well-known/jwks.json"
TOKEN_URL = "https://oauth.telegram.org/token"
#: Чем Telegram подписывает ID-токены (RS256 — по умолчанию)
ALGORITHMS = frozenset({"RS256", "ES256", "EdDSA", "ES256K"})
#: Фото из Telegram — не повод держать вход дольше пары секунд
PICTURE_TIMEOUT = httpx.Timeout(5.0)
#: Сколько перенаправлений пройти за фото: t.me/i/userpic отвечает 302
#: на cdn*.telesco.pe, больше одного-двух не бывает
PICTURE_HOPS = 3
#: Сколько секунд после выдачи ID-токен ещё годится для входа. Окно Telegram
#: отдаёт его приложению сразу, а с запасом на медленную сеть и повтор хватает
#: десяти минут — не часа, на который токен выписан
MAX_TOKEN_AGE_SEC = 10 * 60
#: Откуда качаем фото профиля. Адрес подписан Telegram, но сервер всё равно
#: ходит только к Telegram: сторонний или внутренний адрес в токене не должен
#: превращать вход в запрос от имени сервера куда угодно
PICTURE_HOSTS = ("telesco.pe", "telegram.org", "t.me", "cdn-telegram.org")

_PHONE = re.compile(r"^\+\d{8,15}$")
_NICK = re.compile(r"^[A-Za-z0-9_]{4,40}$")
#: Аккаунт Telegram — положительное число в пределах bigint
_MAX_ID = 2**63 - 1


@dataclass(frozen=True)
class TelegramUser:
    id: int
    #: Полное имя как в Telegram — правило имени применяет вход (names.py)
    name: str
    username: str | None
    picture: str | None
    #: +998901234567, только подтверждённый Telegram
    phone: str | None


def _telegram_id(claims: dict) -> int:
    raw = claims.get("id")
    if isinstance(raw, bool):
        raise TokenInvalid("id не число")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise TokenInvalid("нет id") from None
    if not 0 < value <= _MAX_ID:
        raise TokenInvalid("id вне пределов")
    return value


def _phone(claims: dict) -> str | None:
    """Номер из токена к нашему виду. Telegram пишет его без плюса
    (971577777777), вход по номеру хранит с плюсом — иначе один человек
    не нашёлся бы двумя способами"""
    if claims.get("phone_number_verified") not in (True, "true"):
        return None
    digits = re.sub(r"\D", "", str(claims.get("phone_number") or ""))
    phone = f"+{digits}"
    return phone if _PHONE.match(phone) else None


def _name(claims: dict) -> str:
    name = claims.get("name")
    if not isinstance(name, str) or not name.strip():
        parts = [claims.get("given_name"), claims.get("family_name")]
        name = " ".join(p for p in parts if isinstance(p, str) and p.strip())
    return name or ""


def _telegram_picture(url: str) -> bool:
    """Адрес фото ведёт к Telegram: https, обычный порт, домен из списка.
    Голый IP не годится, даже если он чей-то из Telegram"""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return False
    host = (parsed.host or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.userinfo:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return any(host == d or host.endswith(f".{d}") for d in PICTURE_HOSTS)


def _username(claims: dict) -> str | None:
    nick = claims.get("preferred_username")
    if isinstance(nick, str):
        nick = nick.strip().lstrip("@")
        if _NICK.match(nick):
            return nick
    return None


class TelegramLogin:
    def __init__(
        self,
        bot_id: str,
        client_id: str = "",
        client_secret: str = "",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.bot_id = bot_id
        self.client_id = client_id or bot_id
        self.client_secret = client_secret
        self._http = http or httpx.AsyncClient(timeout=10)
        self.jwks = Jwks(JWKS_URL, self._http)

    @property
    def can_exchange(self) -> bool:
        return bool(self.client_secret)

    async def verify(self, id_token: str) -> TelegramUser:
        """Проверенный человек или TokenInvalid / ProviderUnavailable"""
        claims = await verify(
            id_token,
            self.jwks,
            issuer=ISSUER,
            audience=self.bot_id,
            algorithms=ALGORITHMS,
            max_age=MAX_TOKEN_AGE_SEC,
        )
        picture = claims.get("picture")
        return TelegramUser(
            id=_telegram_id(claims),
            name=_name(claims),
            username=_username(claims),
            picture=picture if isinstance(picture, str) else None,
            phone=_phone(claims),
        )

    async def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> str:
        """Код авторизации → ID-токен. Отказ Telegram — TokenInvalid: код
        чужой, просроченный или верификатор не тот"""
        try:
            response = await self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self.client_id,
                    "code_verifier": code_verifier,
                },
                auth=(self.client_id, self.client_secret),
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"token: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise ProviderUnavailable(f"token: {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            body = {}
        token = body.get("id_token") if response.status_code == 200 else None
        if not isinstance(token, str) or not token:
            log.info("вход через Telegram: код не обменялся (%s %s)",
                     response.status_code, body.get("error"))
            raise TokenInvalid("код не обменялся")
        return token

    async def picture(self, url: str | None) -> bytes | None:
        """Фото профиля Telegram по адресу из токена или None.

        Не вышло — не ошибка входа: человек просто останется без фото.
        Качаем не больше предела фото анкеты — дальше его всё равно
        не примет store_avatar. Адрес из токена — `t.me/i/userpic/…`, и он
        отвечает перенаправлением на сервер картинок Telegram: переходы
        проходим сами, и каждый адрес по дороге — только Telegram, иначе
        перенаправление увело бы запрос с проверенного домена"""
        for _ in range(PICTURE_HOPS + 1):
            if not url or not _telegram_picture(url):
                if url:
                    log.info("фото из Telegram: адрес не Telegram, не качаем")
                return None
            try:
                async with self._http.stream(
                    "GET", url, timeout=PICTURE_TIMEOUT, follow_redirects=False
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        url = str(response.url.join(location)) if location else None
                        continue
                    if response.status_code != 200:
                        return None
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_AVATAR_BYTES:
                        return None
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data += chunk
                        if len(data) > MAX_AVATAR_BYTES:
                            return None
                    return bytes(data)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                log.info("фото из Telegram не скачалось: %s", type(exc).__name__)
                return None
        log.info("фото из Telegram: слишком много перенаправлений")
        return None


_login: TelegramLogin | None = None


def get_telegram_login() -> TelegramLogin | None:
    """Проверка входа через Telegram. None — вход выключен (нет ID бота).

    Отдельной зависимостью: тесты подставляют свою, с тестовыми ключами
    и без сети"""
    global _login
    if not settings.tg_login_bot_id:
        return None
    if _login is None:
        _login = TelegramLogin(
            settings.tg_login_bot_id,
            settings.tg_login_client_id,
            settings.tg_login_client_secret,
        )
    return _login
