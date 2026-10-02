"""Вход через Telegram и Apple, привязка Telegram, правило имени, отзыв Apple.

В сеть не ходим: Telegram и Apple подменены httpx.MockTransport, а токены
подписаны своими тестовыми ключами — проверка подписи, издателя, получателя
и срока идёт настоящая, с настоящими JWKS в ответах подмены.
"""

import base64
import hashlib
import io
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import RSAAlgorithm
from PIL import Image
from sqlalchemy import delete, select

from app.auth import apple as apple_mod
from app.auth import jwks as jwks_mod
from app.auth.apple import AppleSignIn, get_apple, revoke_due
from app.auth.names import clean_name, split_name
from app.auth.sealed import unseal
from app.auth.telegram_login import TelegramLogin, get_telegram_login
from app.auth.tokens import new_token
from app.config import AVATARS_DIR, settings
from app.db import SessionLocal
from app.main import app
from app.models import (
    ApiEvent,
    AppleRevoke,
    LoginRequest,
    PushOutbox,
    Room,
    RoomMember,
    TgJob,
    User,
    UserSession,
)

BOT = "777000123"
TG_ID = 987654321
PHONE = "+998901234567"
PICTURE = "https://cdn4.telesco.pe/file/avatar.jpg"
#: Так фото приходит в токене: t.me отвечает 302 на сервер картинок
USERPIC = "https://t.me/i/userpic/320/avatar.jpg"
USERPIC_ELSEWHERE = "https://t.me/i/userpic/320/elsewhere.jpg"
USERPIC_LOOP = "https://t.me/i/userpic/320/loop.jpg"
TG_ISS = "https://oauth.telegram.org"
TG_JWKS = "https://oauth.telegram.org/.well-known/jwks.json"
TG_TOKEN = "https://oauth.telegram.org/token"
APPLE_ISS = "https://appleid.apple.com"
APPLE_JWKS = "https://appleid.apple.com/auth/keys"
APPLE_TOKEN = "https://appleid.apple.com/auth/token"
APPLE_REVOKE = "https://appleid.apple.com/auth/revoke"
BUNDLE = "uz.sayr.ios"
TEAM = "TEAM123456"
KEY_ID = "KEY1234567"
REDIRECT = "https://app777000123-login.tg.dev/tglogin"
#: Случайная строка приложения для входа с Apple: Apple получает её SHA-256,
#: сервер — её саму
APPLE_NONCE = "raw-nonce-123"
APPLE_NONCE_HASH = hashlib.sha256(APPLE_NONCE.encode()).hexdigest()
H = {"X-Device-Id": "dev-login-1", "X-Sayr-App": "ios/1.8.0"}

# Ключи — один раз на файл: RSA генерируется заметное время
TG_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TG_KEY_NEW = rsa.generate_private_key(public_exponent=65537, key_size=2048)
STRANGER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
APPLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
#: Ключ Sign in with Apple (.p8) — им подписывается клиентский секрет
SIWA_KEY = ec.generate_private_key(ec.SECP256R1())
SIWA_PEM = SIWA_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


def _jwk(private, kid: str) -> dict:
    data = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    return data | {"kid": kid, "alg": "RS256", "use": "sig"}


def _jpeg(side: int = 800) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (side, side), (47, 93, 63)).save(buf, "JPEG")
    return buf.getvalue()


def tg_token(key=TG_KEY, kid="tg-1", alg="RS256", **over) -> str:
    now = int(time.time())
    claims = {
        "iss": TG_ISS,
        "aud": BOT,
        "sub": "1234123412341234123",
        "iat": now,
        "exp": now + 3600,
        "id": TG_ID,
        "name": "Abdulaziz Mannopov",
        "preferred_username": "aziz_hikes",
        "picture": PICTURE,
        "phone_number": PHONE.lstrip("+"),
        "phone_number_verified": True,
    } | over
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def apple_token(key=APPLE_KEY, kid="ap-1", **over) -> str:
    now = int(time.time())
    claims = {
        "iss": APPLE_ISS,
        "aud": BUNDLE,
        "sub": "001234.4b1b2c3d4e5f60718293a4b5c6d7e8f9.1234",
        "iat": now,
        "exp": now + 600,
        "email": "abc@privaterelay.appleid.com",
        "nonce": APPLE_NONCE_HASH,
    } | over
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class Net:
    """Telegram и Apple, какими их видит сервер: JWKS, обмен кода, фото, отзыв"""

    def __init__(self) -> None:
        self.tg_keys = [_jwk(TG_KEY, "tg-1")]
        self.apple_keys = [_jwk(APPLE_KEY, "ap-1")]
        self.jwks_hits = {"tg": 0, "apple": 0}
        self.picture: bytes | None = _jpeg()
        self.down = False
        self.tg_token_requests: list[httpx.Request] = []
        self.tg_code_ok = True
        self.apple_token_requests: list[dict] = []
        self.apple_token_status = 200
        self.revokes: list[dict] = []
        self.revoke_status = 200
        #: Все адреса, куда сервер пытался сходить, и удачно, и нет
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if self.down:
            raise httpx.ConnectError("нет сети", request=request)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if url == TG_JWKS:
            self.jwks_hits["tg"] += 1
            return httpx.Response(200, json={"keys": self.tg_keys})
        if url == APPLE_JWKS:
            self.jwks_hits["apple"] += 1
            return httpx.Response(200, json={"keys": self.apple_keys})
        if url == TG_TOKEN:
            self.tg_token_requests.append(request)
            if not self.tg_code_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": tg_token(), "expires_in": 3600})
        if url == USERPIC:
            return httpx.Response(302, headers={"location": PICTURE})
        if url == USERPIC_ELSEWHERE:
            return httpx.Response(302, headers={"location": "https://evil.example/a.jpg"})
        if url == USERPIC_LOOP:
            return httpx.Response(302, headers={"location": "/i/userpic/320/loop.jpg"})
        if url == PICTURE:
            if self.picture is None:
                return httpx.Response(404)
            return httpx.Response(200, content=self.picture)
        if url == APPLE_TOKEN:
            self.apple_token_requests.append(form)
            if self.apple_token_status != 200:
                return httpx.Response(self.apple_token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"refresh_token": "r-token", "id_token": "x"})
        if url == APPLE_REVOKE:
            self.revokes.append(form)
            return httpx.Response(self.revoke_status)
        return httpx.Response(404)


@pytest.fixture
def net():
    fake = Net()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    fake.tg = TelegramLogin(BOT, "", "bot-secret", http=http)
    fake.apple = AppleSignIn(BUNDLE, TEAM, KEY_ID, SIWA_PEM, http=http)
    app.dependency_overrides[get_telegram_login] = lambda: fake.tg
    app.dependency_overrides[get_apple] = lambda: fake.apple
    # Отзыв при удалении зовёт get_apple() сам, мимо зависимостей FastAPI
    saved, apple_mod._apple = apple_mod._apple, fake.apple
    yield fake
    apple_mod._apple = saved
    app.dependency_overrides.pop(get_telegram_login, None)
    app.dependency_overrides.pop(get_apple, None)


@pytest.fixture(autouse=True)
async def clean():
    yield
    async with SessionLocal() as session:
        for model in (AppleRevoke, TgJob, PushOutbox, RoomMember, Room, UserSession, User,
                      LoginRequest):
            await session.execute(delete(model))
        await session.commit()


async def _user(**fields) -> tuple[dict, int]:
    """Человек с токеном прямо в базе: (заголовки, id)"""
    token, digest = new_token()
    async with SessionLocal() as session:
        user = User(**fields)
        session.add(user)
        await session.flush()
        session.add(UserSession(user_id=user.id, token_hash=digest, device_id="dev-login-0"))
        await session.commit()
        return {"Authorization": f"Bearer {token}", "X-Device-Id": "dev-login-0"}, user.id


async def _get(user_id: int) -> User | None:
    async with SessionLocal() as session:
        return await session.get(User, user_id)


async def _tg(client, token: str | None = None, **body):
    payload = {"id_token": token or tg_token()} | body
    return await client.post("/api/v1/auth/telegram", json=payload, headers=H)


# --- Правило имени ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ali", "Ali"),
        ("Абдулазиз", "Абдулазиз"),
        ("  Азиз   Каримов ", "Азиз Каримов"),
        # Декоративные «шрифты» — к обычным буквам, а не в корзину
        ("𝓐𝓵𝓲", "Ali"),
        ("𝐀𝐳𝐢𝐳", "Aziz"),
        ("Ａｌｉ", "Ali"),
        # Узбекские o‘ g‘ — обоими апострофами, и ʻ из орфографии
        ("Gʻayrat", "Gʻayrat"),
        ("G‘ayrat", "G‘ayrat"),
        ("Oʻgʻiloy", "Oʻgʻiloy"),
        ("Qo‘chqorov", "Qo‘chqorov"),
        ("Ўткир Қодиров", "Ўткир Қодиров"),
        ("Анна-Мария", "Анна-Мария"),
        ("O'Brien", "O'Brien"),
        ("José", "José"),
        # Без узбекской раскладки o‘ g‘ набирают через ` и ´ — к виду приложений
        ("O`tkir", "O‘tkir"),
        ("G´ulnora", "G‘ulnora"),
        ("Qo`chqorov", "Qo‘chqorov"),
        ("", ""),
        # Символы вместо имени — не имя
        ("✨Ali✨", None),
        ("Ali 😎", None),
        ("ᏗᏝᎥ", None),
        ("A.Karimov", None),
        ("Ali2", None),
        ("A", None),
        ("--", None),
        ("Αλέξης", None),
        ("a" * 31, None),
    ],
)
def test_правило_имени(raw, expected):
    assert clean_name(raw) == expected


def test_имя_из_telegram_делится_на_имя_и_фамилию():
    assert split_name("Abdulaziz Mannopov") == ("Abdulaziz", "Mannopov")
    assert split_name("Gʻayrat Qoʻchqorov Ogʻli") == ("Gʻayrat", "Qoʻchqorov Ogʻli")
    assert split_name("Aziz") == ("Aziz", "")
    assert split_name("✨Ali✨") == ("", "")
    # Целиком проходит, но одна буква — не имя
    assert split_name("A Karimov") == ("", "")
    assert split_name(None) == ("", "")


# --- Вход через Telegram ----------------------------------------------------


async def test_telegram_новый_аккаунт_с_анкетой_из_telegram(client, net):
    resp = await _tg(client, tg_token(name="𝓐𝓫𝓭𝓾𝓵𝓪𝔃𝓲𝔃 Mannopov"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"]
    user = body["user"]
    assert user["first_name"] == "Abdulaziz"
    assert user["last_name"] == "Mannopov"
    assert user["telegram_username"] == "aziz_hikes"
    # Номер из Telegram — в нашем виде, с плюсом: вход по номеру найдёт того же
    assert user["phone"] == PHONE
    assert user["login_methods"] == ["telegram", "phone"]
    # Анкету человек ещё проверит сам: она заполнена, но не «заполнена им»
    assert user["profile_filled"] is False
    assert user["avatar_url"]
    assert (AVATARS_DIR / user["avatar_url"].rsplit("/", 1)[1]).exists()

    async with SessionLocal() as session:
        row = await session.get(User, user["id"])
        assert row.telegram_id == TG_ID
        sessions = (await session.execute(select(UserSession))).scalars().all()
    assert [(s.device_id, s.platform) for s in sessions] == [("dev-login-1", "ios")]


async def test_telegram_повторный_вход_находит_того_же_и_анкету_не_трогает(client, net):
    first = (await _tg(client)).json()["user"]
    # Сменил имя в Telegram — анкета Sayr уже его, её не переписываем
    second = await _tg(client, tg_token(name="Other Name", preferred_username="other_nick"))
    assert second.status_code == 200
    assert second.json()["user"]["id"] == first["id"]
    assert second.json()["user"]["first_name"] == "Abdulaziz"
    assert second.json()["user"]["telegram_username"] == "aziz_hikes"
    async with SessionLocal() as session:
        assert len((await session.execute(select(User))).scalars().all()) == 1
        assert len((await session.execute(select(UserSession))).scalars().all()) == 2


async def test_двойное_нажатие_не_заводит_двух_аккаунтов(client, net):
    import asyncio

    tg = await asyncio.gather(_tg(client), _tg(client))
    assert [r.status_code for r in tg] == [200, 200]
    assert tg[0].json()["user"]["id"] == tg[1].json()["user"]["id"]
    ap = await asyncio.gather(_apple(client), _apple(client))
    assert [r.status_code for r in ap] == [200, 200]
    assert ap[0].json()["user"]["id"] == ap[1].json()["user"]["id"]
    async with SessionLocal() as session:
        assert len((await session.execute(select(User))).scalars().all()) == 2


async def test_telegram_находит_аккаунт_по_подтверждённому_номеру(client, net):
    _, user_id = await _user(
        phone=PHONE, first_name="Азиз", profile_filled_at=datetime.now(timezone.utc)
    )
    resp = await _tg(client)
    assert resp.status_code == 200, resp.text
    user = resp.json()["user"]
    assert user["id"] == user_id
    # Свою анкету человек уже заполнил — Telegram её не переписывает
    assert user["first_name"] == "Азиз"
    assert user["login_methods"] == ["telegram", "phone"]
    assert (await _get(user_id)).telegram_id == TG_ID


async def test_telegram_неподтверждённый_номер_не_склеивает(client, net):
    _, user_id = await _user(phone=PHONE)
    resp = await _tg(client, tg_token(phone_number_verified=False))
    user = resp.json()["user"]
    assert user["id"] != user_id
    assert user["phone"] == ""
    assert user["login_methods"] == ["telegram"]


async def test_telegram_номер_у_аккаунта_с_другим_telegram_не_открывает_его(client, net):
    """Номер переехал на новый аккаунт Telegram: чужой аккаунт Sayr по такому
    совпадению не открываем — новый, без номера"""
    _, user_id = await _user(phone=PHONE, telegram_id=111)
    resp = await _tg(client)
    user = resp.json()["user"]
    assert user["id"] != user_id
    assert user["phone"] == ""
    assert (await _get(user_id)).telegram_id == 111


async def test_telegram_без_номера_и_номер_приходит_позже(client, net):
    first = (await _tg(client, tg_token(phone_number=None, phone_number_verified=None))).json()
    assert first["user"]["login_methods"] == ["telegram"]
    # Во второй раз человек разрешил номер — он ничей, ложится к аккаунту
    second = (await _tg(client)).json()
    assert second["user"]["id"] == first["user"]["id"]
    assert second["user"]["phone"] == PHONE


@pytest.mark.parametrize(
    ("name", "first", "last"),
    [
        ("✨Ali✨", "", ""),
        ("ᏗᏝᎥ", "", ""),
        ("Ali 😎", "", ""),
        ("A.Karimov", "", ""),
        ("𝐀𝐳𝐢𝐳", "Aziz", ""),
        ("Gʻayrat Qoʻchqorov", "Gʻayrat", "Qoʻchqorov"),
    ],
)
async def test_telegram_имя_символами_не_переносим(client, net, name, first, last):
    user = (await _tg(client, tg_token(name=name))).json()["user"]
    assert (user["first_name"], user["last_name"]) == (first, last)


async def test_telegram_фото_через_перенаправление_t_me(client, net):
    resp = await _tg(client, tg_token(picture=USERPIC))
    assert resp.status_code == 200
    assert resp.json()["user"]["avatar_url"]
    assert net.requested.count(PICTURE) == 1


async def test_telegram_перенаправление_не_к_telegram_не_качаем(client, net):
    resp = await _tg(client, tg_token(picture=USERPIC_ELSEWHERE))
    assert resp.status_code == 200
    assert resp.json()["user"]["avatar_url"] is None
    assert "https://evil.example/a.jpg" not in net.requested


async def test_telegram_петля_перенаправлений_кончается(client, net):
    resp = await _tg(client, tg_token(picture=USERPIC_LOOP))
    assert resp.status_code == 200
    assert resp.json()["user"]["avatar_url"] is None
    assert net.requested.count(USERPIC_LOOP) <= 4


async def test_telegram_фото_и_у_прежнего_аккаунта_без_анкеты(client, net):
    """Аккаунт уже был (вошёл раньше, фото не пришло), анкету не заполнял —
    следующий вход через Telegram ставит фото"""
    net.picture = None
    first = (await _tg(client)).json()["user"]
    assert first["avatar_url"] is None
    net.picture = _jpeg()
    again = (await _tg(client)).json()["user"]
    assert again["id"] == first["id"]
    assert again["avatar_url"]


async def test_telegram_фото_не_скачалось_вход_всё_равно_есть(client, net):
    net.picture = None
    resp = await _tg(client)
    assert resp.status_code == 200
    assert resp.json()["user"]["avatar_url"] is None


async def test_telegram_фото_не_картинка_вход_всё_равно_есть(client, net):
    net.picture = b"<html>not an image</html>"
    resp = await _tg(client)
    assert resp.status_code == 200
    assert resp.json()["user"]["avatar_url"] is None


async def test_telegram_фото_тяжелее_предела_не_качаем(client, net, monkeypatch):
    from app.auth import telegram_login

    monkeypatch.setattr(telegram_login, "MAX_AVATAR_BYTES", 1000)
    resp = await _tg(client)
    assert resp.status_code == 200
    assert resp.json()["user"]["avatar_url"] is None


@pytest.mark.parametrize(
    "token",
    [
        # Чужая подпись под нашим kid
        lambda: tg_token(key=STRANGER),
        # Токен другому боту
        lambda: tg_token(aud="555"),
        # Просрочен — дальше допуска по часам
        lambda: tg_token(exp=int(time.time()) - 600, iat=int(time.time()) - 4000),
        # Не Telegram выдал
        lambda: tg_token(iss="https://evil.example"),
        # Без аккаунта Telegram
        lambda: tg_token(id=None),
        # Без подписи вовсе
        lambda: jwt.encode({"iss": TG_ISS, "aud": BOT, "id": TG_ID}, None, algorithm="none"),
        "not-a-jwt",
    ],
)
async def test_telegram_плохой_токен_не_пускает(client, net, token):
    resp = await _tg(client, token() if callable(token) else token)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "telegram_token_invalid"}
    async with SessionLocal() as session:
        assert (await session.execute(select(User))).first() is None


async def test_telegram_допуск_по_часам(client, net):
    """Часы телефона и Telegram расходятся на секунды — это не повод не пустить"""
    resp = await _tg(client, tg_token(exp=int(time.time()) - 20))
    assert resp.status_code == 200


async def test_telegram_aud_числом_тоже_годится(client, net):
    resp = await _tg(client, tg_token(aud=int(BOT)))
    assert resp.status_code == 200


async def test_telegram_новый_ключ_перечитывает_jwks_но_не_чаще_минуты(client, net, monkeypatch):
    assert (await _tg(client)).status_code == 200
    assert net.jwks_hits["tg"] == 1
    # Ещё раз — из кэша
    assert (await _tg(client)).status_code == 200
    assert net.jwks_hits["tg"] == 1
    # Незнакомый kid сразу после чтения — не повод ходить в Telegram снова:
    # иначе поток выдуманных kid превращал бы нас в прокси
    net.tg_keys.append(_jwk(TG_KEY_NEW, "tg-2"))
    resp = await _tg(client, tg_token(key=TG_KEY_NEW, kid="tg-2"))
    assert resp.status_code == 401
    assert net.jwks_hits["tg"] == 1
    # Минута прошла — Telegram выкатил новый ключ, перечитываем и пускаем
    monkeypatch.setattr(jwks_mod, "REFETCH_EVERY_SEC", 0)
    resp = await _tg(client, tg_token(key=TG_KEY_NEW, kid="tg-2"))
    assert resp.status_code == 200
    assert net.jwks_hits["tg"] == 2


async def test_telegram_не_ответил_это_не_плохой_токен(client, net):
    net.down = True
    resp = await _tg(client)
    assert resp.status_code == 502
    assert resp.json() == {"detail": "telegram_unavailable"}


async def test_telegram_лежит_ключи_не_перечитываем_чаще_минуты(client, net, monkeypatch):
    """Неудачная попытка тоже считается: иначе при лежащем Telegram каждый
    вход с выдуманным kid ждал бы свой таймаут, стоя за замком"""
    net.down = True
    for n in range(4):
        resp = await _tg(client, tg_token(kid=f"garbage-{n}"))
        assert resp.status_code == 502
    assert net.requested.count(TG_JWKS) == 1
    # Telegram ожил, но минута не прошла — всё ещё «нет связи», а не «плохой токен»
    net.down = False
    assert (await _tg(client)).status_code == 502
    assert net.requested.count(TG_JWKS) == 1
    monkeypatch.setattr(jwks_mod, "REFETCH_EVERY_SEC", 0)
    assert (await _tg(client)).status_code == 200
    assert net.requested.count(TG_JWKS) == 2


async def test_telegram_лежит_а_ключ_знакомый_пускаем_по_старому_списку(
    client, net, monkeypatch
):
    assert (await _tg(client)).status_code == 200
    # Список устарел, а Telegram как раз лежит: знакомый ключ всё равно годится,
    # и за списком ходим один раз в минуту, а не на каждый вход
    monkeypatch.setattr(jwks_mod, "KEYS_TTL_SEC", 0)
    net.tg.jwks._tried_at -= 120  # с прошлого чтения прошло две минуты
    net.down = True
    assert (await _tg(client)).status_code == 200
    assert (await _tg(client)).status_code == 200
    assert net.requested.count(TG_JWKS) == 2


async def test_telegram_старый_токен_не_пускает(client, net):
    """ID-токен выписан на час, а nonce библиотеки Telegram не передают:
    принимаем только свежий — утёкший не открывает аккаунт весь этот час"""
    now = int(time.time())
    assert (await _tg(client, tg_token(iat=now - 9 * 60))).status_code == 200
    for token in (tg_token(iat=now - 12 * 60), tg_token(iat=None)):
        resp = await _tg(client, token)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "telegram_token_invalid"}


@pytest.mark.parametrize(
    "picture",
    [
        "https://10.0.0.5:8443/admin",
        "https://127.0.0.1/avatar.jpg",
        "https://evil.example/avatar.jpg",
        "https://cdn4.telesco.pe.evil.example/avatar.jpg",
        "https://cdn4.telesco.pe:8443/file/avatar.jpg",
        "http://cdn4.telesco.pe/file/avatar.jpg",
    ],
)
async def test_telegram_фото_только_с_адресов_telegram(client, net, picture):
    resp = await _tg(client, tg_token(picture=picture))
    assert resp.status_code == 200
    assert resp.json()["user"]["avatar_url"] is None
    assert picture not in net.requested


async def test_telegram_номер_проверяющих_не_ищет_и_не_записывается(client, net, monkeypatch):
    """Код к номеру проверяющих роздан магазинам: совпавший с ним номер
    из Telegram не открывает их аккаунт и не записывается никому"""
    monkeypatch.setattr(settings, "login_test_phone", "+998 90 123-45-67")
    first = (await _tg(client)).json()["user"]
    assert first["phone"] == ""
    assert first["login_methods"] == ["telegram"]
    _, reviewer_id = await _user(phone=PHONE, first_name="Review")
    other = await _tg(client, tg_token(id=TG_ID + 1))
    assert other.status_code == 200
    assert other.json()["user"]["id"] not in (reviewer_id, first["id"])
    assert (await _get(reviewer_id)).telegram_id is None


async def test_telegram_выключен_без_id_бота(client):
    app.dependency_overrides[get_telegram_login] = lambda: None
    try:
        resp = await client.post("/api/v1/auth/telegram", json={"id_token": "x"})
    finally:
        app.dependency_overrides.pop(get_telegram_login, None)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "telegram_login_off"}


async def test_telegram_по_умолчанию_выключен(client, monkeypatch):
    monkeypatch.setattr(settings, "tg_login_bot_id", "")
    resp = await client.post("/api/v1/auth/telegram", json={"id_token": "x"})
    assert resp.status_code == 503


async def test_telegram_код_меняется_на_токен_с_pkce(client, net):
    resp = await client.post(
        "/api/v1/auth/telegram",
        json={"code": "auth-code", "code_verifier": "v" * 43, "redirect_uri": REDIRECT},
        headers=H,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["login_methods"] == ["telegram", "phone"]
    [request] = net.tg_token_requests
    expected = base64.b64encode(f"{BOT}:bot-secret".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    form = parse_qs(request.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["auth-code"]
    assert form["code_verifier"] == ["v" * 43]
    assert form["redirect_uri"] == [REDIRECT]
    assert form["client_id"] == [BOT]


async def test_telegram_чужой_код_не_пускает(client, net):
    net.tg_code_ok = False
    resp = await client.post(
        "/api/v1/auth/telegram",
        json={"code": "stolen", "code_verifier": "v" * 43, "redirect_uri": REDIRECT},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "telegram_token_invalid"}


async def test_telegram_без_токена_и_кода(client, net):
    resp = await client.post("/api/v1/auth/telegram", json={"code": "only-code"})
    assert resp.status_code == 422
    assert resp.json() == {"detail": "id_token_or_code_required"}


# --- Привязка Telegram ------------------------------------------------------


async def test_привязать_telegram_к_аккаунту_по_номеру(client, net):
    h, user_id = await _user(phone="+998935550000")
    resp = await client.post(
        "/api/v1/me/telegram", json={"id_token": tg_token(phone_number=None)}, headers=h
    )
    assert resp.status_code == 200, resp.text
    user = resp.json()
    assert user["id"] == user_id
    assert user["login_methods"] == ["telegram", "phone"]
    # Анкета была пустой и нетронутой — дополнили из Telegram
    assert user["first_name"] == "Abdulaziz"
    assert user["telegram_username"] == "aziz_hikes"
    assert (await _get(user_id)).telegram_id == TG_ID
    # Второй раз — то же самое, без ошибки
    again = await client.post("/api/v1/me/telegram", json={"id_token": tg_token()}, headers=h)
    assert again.status_code == 200


async def test_привязать_занятый_telegram_нельзя(client, net):
    await _user(telegram_id=TG_ID)
    h, user_id = await _user(apple_sub="apple-1")
    resp = await client.post("/api/v1/me/telegram", json={"id_token": tg_token()}, headers=h)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "telegram_taken"}
    assert (await _get(user_id)).telegram_id is None


async def test_привязка_без_входа(client, net):
    resp = await client.post("/api/v1/me/telegram", json={"id_token": tg_token()})
    assert resp.status_code == 401


async def test_привязка_с_плохим_токеном_не_выкидывает_из_аккаунта(client, net):
    """401 на запрос с ключом сессии приложения понимают как «сессия
    кончилась» и выходят из аккаунта — неудачная привязка отвечает 400"""
    h, _ = await _user(apple_sub="apple-1")
    for token in (tg_token(key=STRANGER), tg_token(iat=int(time.time()) - 3000)):
        resp = await client.post("/api/v1/me/telegram", json={"id_token": token}, headers=h)
        assert resp.status_code == 400
        assert resp.json() == {"detail": "telegram_token_invalid"}
    # Сессия жива
    assert (await client.get("/api/v1/me", headers=h)).status_code == 200


async def test_привязать_второй_telegram_нельзя(client, net):
    """Уже привязанный Telegram молча не меняется: иначе украденный ключ
    сессии становился бы постоянным входом через чужой Telegram"""
    h, user_id = await _user(apple_sub="apple-3", telegram_id=111)
    resp = await client.post("/api/v1/me/telegram", json={"id_token": tg_token()}, headers=h)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "telegram_already_linked"}
    assert (await _get(user_id)).telegram_id == 111


async def test_привязка_номер_заняли_параллельно(client, net, monkeypatch):
    """Пока проверяли, что номер из Telegram ничей, его занял параллельный
    вход: Telegram всё равно привязан, номер — нет, и не 500"""
    from app.api import auth_providers

    await _user(phone=PHONE)
    h, user_id = await _user(apple_sub="apple-race")

    async def racy(session, user, phone):
        # Проверка «ничей» прошла раньше, чем номер заняли
        if phone:
            user.phone = phone

    monkeypatch.setattr(auth_providers, "_take_phone", racy)
    resp = await client.post("/api/v1/me/telegram", json={"id_token": tg_token()}, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == ""
    assert resp.json()["login_methods"] == ["telegram", "apple"]
    assert (await _get(user_id)).telegram_id == TG_ID


async def test_привязка_telegram_заняли_параллельно(client, net, monkeypatch):
    """Тот же Telegram привязал себе параллельный вход между проверкой
    и записью — 409, а не 500"""
    from app.api import auth_providers

    await _user(telegram_id=TG_ID)
    h, user_id = await _user(apple_sub="apple-race-2")
    real, calls = auth_providers._one, []

    async def blind_first(session, *where, **kw):
        calls.append(where)
        # Первый поиск владельца «не видит» уже записанного
        return None if len(calls) == 1 else await real(session, *where, **kw)

    monkeypatch.setattr(auth_providers, "_one", blind_first)
    resp = await client.post("/api/v1/me/telegram", json={"id_token": tg_token()}, headers=h)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "telegram_taken"}
    assert (await _get(user_id)).telegram_id is None


# --- Комнаты знают аккаунт Telegram ----------------------------------------


async def test_комната_сразу_знает_аккаунт_telegram(client, net, monkeypatch):
    monkeypatch.setattr(settings, "rooms_open", True)
    # Год рождения — без него в комнату теперь не пускают и своих
    organizer, organizer_id = await _user(
        phone="+998935550001", first_name="Азиз", birth_year=1995, telegram_id=501
    )
    friend, friend_id = await _user(
        apple_sub="apple-2", first_name="Ali", birth_year=1996, telegram_id=502
    )
    day = (datetime.now(timezone.utc) + timedelta(days=5)).date().isoformat()
    room = await client.post(
        "/api/v1/rooms", json={"place": "test-peak", "day": day, "is_open": False}, headers=organizer
    )
    assert room.status_code == 201, room.text
    code = room.json()["code"]
    async with SessionLocal() as session:
        row = (await session.execute(select(Room).where(Room.code == code))).scalar_one()
        row.tg_state, row.tg_chat_id, row.tg_access_hash = "ready", 42, 4242
        await session.commit()
    invite = room.json()["invite_url"].rsplit("/", 1)[1]
    joined = await client.post(f"/api/v1/invites/{invite}/join", headers=friend)
    assert joined.status_code == 200, joined.text

    async with SessionLocal() as session:
        members = {
            m.user_id: m.tg_user_id
            for m in (await session.execute(select(RoomMember))).scalars()
        }
    assert members == {organizer_id: 501, friend_id: 502}

    # Ушёл — Sayr Admin знает, кого убрать, хоть по ссылке тот ещё не входил
    left = await client.post(f"/api/v1/rooms/{code}/leave", headers=friend)
    assert left.status_code == 204
    async with SessionLocal() as session:
        kick = (
            await session.execute(select(TgJob).where(TgJob.kind == "kick"))
        ).scalar_one()
    assert kick.payload["tg_user_id"] == 502


async def test_привязанный_telegram_доезжает_до_строк_участия(client, net):
    h, user_id = await _user(phone="+998935550002", first_name="Азиз")
    async with SessionLocal() as session:
        from app.models import Place

        place = (await session.execute(select(Place).limit(1))).scalar_one()
        room = Room(
            code="tglink1",
            invite="tglinkinvite1",
            place_id=place.id,
            organizer_id=user_id,
            day=datetime.now(timezone.utc).date(),
        )
        session.add(room)
        await session.flush()
        session.add(
            RoomMember(
                room_id=room.id, user_id=user_id, role="organizer", status="joined",
                source="organizer",
            )
        )
        await session.commit()
    resp = await client.post(
        "/api/v1/me/telegram", json={"id_token": tg_token(phone_number=None)}, headers=h
    )
    assert resp.status_code == 200
    async with SessionLocal() as session:
        member = (await session.execute(select(RoomMember))).scalar_one()
    assert member.tg_user_id == TG_ID


async def test_служба_находит_ключ_участника_по_номеру_аккаунта():
    """Аккаунт со входа через Telegram приходит без ключа доступа: боевая
    прослойка берёт его из списка участников группы, а нет там человека —
    NotParticipant, который служба и так понимает как «уже вышел»"""
    from telethon.tl.functions.channels import EditBannedRequest, GetParticipantsRequest

    from app.tg.api import NotParticipant, TelethonApi

    class Client:
        def __init__(self):
            self.calls = []

        async def __call__(self, request):
            self.calls.append(request)
            if isinstance(request, GetParticipantsRequest):
                return SimpleNamespace(
                    users=[SimpleNamespace(id=502, access_hash=777)],
                    participants=[object()],
                )
            return None

    client = Client()
    api = TelethonApi(client)
    await api.kick((42, 4242), 502, 0)
    bans = [c for c in client.calls if isinstance(c, EditBannedRequest)]
    assert len(bans) == 2
    assert bans[0].participant.user_id == 502
    assert bans[0].participant.access_hash == 777
    with pytest.raises(NotParticipant):
        await api.kick((42, 4242), 999, 0)


# --- Вход с Apple -----------------------------------------------------------


async def _apple(client, token: str | None = None, **body):
    payload = {
        "identity_token": token or apple_token(),
        "authorization_code": "code-1",
        "nonce": APPLE_NONCE,
    } | body
    return await client.post("/api/v1/auth/apple", json=payload, headers=H)


async def test_apple_новый_аккаунт_и_токен_для_отзыва(client, net):
    resp = await _apple(client, first_name="Abdulaziz", last_name="Mannopov")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"]
    user = body["user"]
    assert (user["first_name"], user["last_name"]) == ("Abdulaziz", "Mannopov")
    # Номера у Apple нет — пустая строка, а не null: старые сборки его ждут
    assert user["phone"] == ""
    assert user["login_methods"] == ["apple"]

    row = await _get(user["id"])
    assert row.apple_sub.startswith("001234.")
    # Refresh-токен лежит шифром, а не как есть
    assert row.apple_refresh and "r-token" not in row.apple_refresh
    assert unseal(row.apple_refresh) == "r-token"

    [form] = net.apple_token_requests
    assert form["client_id"] == BUNDLE
    assert form["code"] == "code-1"
    assert form["grant_type"] == "authorization_code"
    # Клиентский секрет — JWT ES256 ключом Sign in with Apple
    secret = form["client_secret"]
    assert jwt.get_unverified_header(secret)["kid"] == KEY_ID
    claims = jwt.decode(secret, SIWA_KEY.public_key(), algorithms=["ES256"], audience=APPLE_ISS)
    assert claims["iss"] == TEAM
    assert claims["sub"] == BUNDLE


async def test_apple_повторный_вход_без_имени_находит_того_же(client, net):
    first = (await _apple(client, first_name="Abdulaziz")).json()["user"]
    # Apple присылает имя только в первый раз
    second = await _apple(client)
    assert second.status_code == 200
    assert second.json()["user"]["id"] == first["id"]
    assert second.json()["user"]["first_name"] == "Abdulaziz"


async def test_apple_имя_символами_не_переносим(client, net):
    user = (await _apple(client, first_name="✨Ali✨", last_name="Karimov")).json()["user"]
    assert (user["first_name"], user["last_name"]) == ("", "")


async def test_apple_nonce(client, net):
    ok = await _apple(client)
    assert ok.status_code == 200
    for token, nonce in [
        # Не та строка
        (apple_token(), "another"),
        # Хеш из самого токена: он виден любому, у кого есть токен
        (apple_token(), APPLE_NONCE_HASH),
        # Nonce в токене нет вовсе — токен выпрошен не нашим приложением
        (apple_token(nonce=None), APPLE_NONCE),
        # Приложение отдало Apple строку как есть, без хеша
        (apple_token(nonce=APPLE_NONCE), APPLE_NONCE),
    ]:
        bad = await _apple(client, token, nonce=nonce)
        assert bad.status_code == 401, nonce
        assert bad.json() == {"detail": "apple_token_invalid"}


async def test_apple_без_nonce_не_пускает(client, net):
    """Без исходной строки утёкший identity token открывал бы аккаунт"""
    resp = await client.post(
        "/api/v1/auth/apple",
        json={"identity_token": apple_token(), "authorization_code": "code-1"},
        headers=H,
    )
    assert resp.status_code == 422
    assert (await _apple(client, nonce="")).status_code == 422
    async with SessionLocal() as session:
        assert (await session.execute(select(User))).first() is None


@pytest.mark.parametrize(
    "token",
    [
        lambda: apple_token(aud="uz.other.app"),
        lambda: apple_token(key=STRANGER),
        lambda: apple_token(iss=TG_ISS),
        lambda: apple_token(exp=int(time.time()) - 600),
    ],
)
async def test_apple_плохой_токен_не_пускает(client, net, token):
    resp = await _apple(client, token())
    assert resp.status_code == 401
    assert resp.json() == {"detail": "apple_token_invalid"}


async def test_apple_без_ключа_вход_есть_отзывать_нечем(client, net):
    net.apple = AppleSignIn(BUNDLE, TEAM, "", None, http=net.apple._http)
    resp = await _apple(client)
    assert resp.status_code == 200
    assert net.apple_token_requests == []
    assert (await _get(resp.json()["user"]["id"])).apple_refresh is None


async def test_apple_код_не_обменялся_вход_всё_равно_есть(client, net):
    net.apple_token_status = 400
    resp = await _apple(client)
    assert resp.status_code == 200
    assert (await _get(resp.json()["user"]["id"])).apple_refresh is None


async def test_удаление_отзывает_доступ_apple(client, net):
    login = (await _apple(client)).json()
    h = {"Authorization": f"Bearer {login['token']}", "X-Device-Id": "dev-login-1"}
    resp = await client.delete("/api/v1/me", headers=h)
    assert resp.status_code == 204
    assert await _get(login["user"]["id"]) is None
    [form] = net.revokes
    assert form["token"] == "r-token"
    assert form["token_type_hint"] == "refresh_token"
    assert form["client_id"] == BUNDLE
    async with SessionLocal() as session:
        assert (await session.execute(select(AppleRevoke))).first() is None


async def test_отзыв_apple_не_прошёл_удаление_всё_равно_есть(client, net):
    login = (await _apple(client)).json()
    h = {"Authorization": f"Bearer {login['token']}", "X-Device-Id": "dev-login-1"}
    net.revoke_status = 500
    resp = await client.delete("/api/v1/me", headers=h)
    assert resp.status_code == 204
    assert await _get(login["user"]["id"]) is None
    async with SessionLocal() as session:
        row = (await session.execute(select(AppleRevoke))).scalar_one()
        assert row.attempts == 1
        assert row.last_error
        assert unseal(row.token) == "r-token"
        # Повтор — ежечасным проходом; Apple ожил
        row.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
    net.revoke_status = 200
    async with SessionLocal() as session:
        assert await revoke_due(session) == 1
    async with SessionLocal() as session:
        assert (await session.execute(select(AppleRevoke))).first() is None
    assert [f["token"] for f in net.revokes] == ["r-token", "r-token"]


async def test_отзыв_ждёт_ключа_и_старше_месяца_не_держится(net):
    from app.auth.sealed import seal

    async with SessionLocal() as session:
        session.add(AppleRevoke(token=seal("fresh")))
        session.add(
            AppleRevoke(
                token=seal("old"), created_at=datetime.now(timezone.utc) - timedelta(days=31)
            )
        )
        await session.commit()
    no_key = AppleSignIn(BUNDLE, TEAM, "", None, http=net.apple._http)
    async with SessionLocal() as session:
        assert await revoke_due(session, no_key) == 0
    async with SessionLocal() as session:
        rows = (await session.execute(select(AppleRevoke))).scalars().all()
    # Без ключа попытка не тратится; месячный — стёрт, даже не отозванный
    assert [(unseal(r.token), r.attempts) for r in rows] == [("fresh", 0)]
    assert net.revokes == []


async def test_удаление_аккаунта_без_номера(client, net):
    login = (await _tg(client, tg_token(phone_number=None))).json()
    h = {"Authorization": f"Bearer {login['token']}", "X-Device-Id": "dev-login-1"}
    assert (await client.delete("/api/v1/me", headers=h)).status_code == 204
    assert await _get(login["user"]["id"]) is None
    # Отзывать нечего — Apple не трогали
    assert net.revokes == []


# --- Анкета: то же правило имени -------------------------------------------


@pytest.mark.parametrize("name", ["✨Ali✨", "Ali2", "A", "A.Karimov", "ᏗᏝᎥ"])
async def test_анкета_не_принимает_символы_вместо_имени(client, name):
    h, user_id = await _user(phone="+998935550003")
    resp = await client.patch("/api/v1/me", json={"first_name": name}, headers=h)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "name_letters_only"}
    resp = await client.patch("/api/v1/me", json={"last_name": name}, headers=h)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "name_letters_only"}
    assert (await _get(user_id)).first_name == ""


@pytest.mark.parametrize(
    ("name", "stored"),
    [("Oʻgʻiloy", "Oʻgʻiloy"), ("G‘ayrat", "G‘ayrat"), ("Анна-Мария", "Анна-Мария"),
     ("𝐀𝐳𝐢𝐳", "Aziz"), ("", "")],
)
async def test_анкета_принимает_настоящие_имена(client, name, stored):
    h, _ = await _user(phone="+998935550004")
    resp = await client.patch("/api/v1/me", json={"first_name": name}, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["first_name"] == stored


async def test_анкета_со_старым_именем_сохраняется(client):
    """Приложения шлют анкету целиком: имя, заведённое до правила, не мешает
    поменять год рождения. Правило — только для меняемого имени"""
    h, user_id = await _user(phone="+998935550006", first_name="Aziz.", last_name="Ali2")
    resp = await client.patch(
        "/api/v1/me",
        json={"first_name": "Aziz.", "last_name": "Ali2", "birth_year": 1990},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["first_name"], resp.json()["last_name"]) == ("Aziz.", "Ali2")
    assert (await _get(user_id)).birth_year == 1990
    resp = await client.patch("/api/v1/me", json={"first_name": "Aziz.."}, headers=h)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "name_letters_only"}
    resp = await client.patch("/api/v1/me", json={"first_name": "O`tkir"}, headers=h)
    assert resp.json()["first_name"] == "O‘tkir"


async def test_способы_входа_у_аккаунта_по_номеру(client):
    h, _ = await _user(phone="+998935550005")
    me = await client.get("/api/v1/me", headers=h)
    assert me.json()["login_methods"] == ["phone"]
    assert me.json()["phone"] == "+998935550005"


async def test_админка_не_показывает_токен_apple(admin_client):
    from app.auth.sealed import seal

    sealed = seal("r-admin")
    _, user_id = await _user(apple_sub="apple-admin", apple_refresh=sealed)
    page = await admin_client.get(f"/admin/user/details/{user_id}")
    assert page.status_code == 200
    assert sealed not in page.text
    assert "apple-admin" in page.text


# --- Статистика: вход, привязка, анкета (02.10.2026) ------------------------

#: Настоящий номер устройства (UUID): только такой сервер пишет в событие
DEVICE = "6b1f0c9e-2d4a-4e8b-9c3f-7a5d1e2b4c68"
HD = {"X-Device-Id": DEVICE, "X-Sayr-App": "ios/1.9.5 ru 26"}


async def _forget_events() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(ApiEvent))
        await session.commit()


async def _counted(kind: str) -> list[tuple[str | None, str | None]]:
    """(ключ, устройство) событий вида — в порядке записи"""
    async with SessionLocal() as session:
        rows = await session.execute(
            select(ApiEvent.slug, ApiEvent.device)
            .where(ApiEvent.kind == kind)
            .order_by(ApiEvent.id)
        )
        return [tuple(r) for r in rows.all()]


async def test_статистика_telegram_новый_и_вернувшийся(client, net):
    await _forget_events()
    for _ in range(2):
        resp = await client.post("/api/v1/auth/telegram", json={"id_token": tg_token()}, headers=HD)
        assert resp.status_code == 200, resp.text
    # Номер устройства — из запроса, как у событий middleware
    assert await _counted("login") == [("telegram:new", DEVICE), ("telegram:back", DEVICE)]


async def test_статистика_telegram_по_номеру_прежнего_аккаунта_это_возвращение(client, net):
    """Аккаунт по номеру уже был — Telegram к нему только привязался"""
    await _forget_events()
    await _user(phone=PHONE)
    assert (await _tg(client)).status_code == 200
    assert [k for k, _ in await _counted("login")] == ["telegram:back"]


async def test_статистика_apple_новый_и_вернувшийся(client, net):
    await _forget_events()
    assert (await _apple(client, first_name="Abdulaziz")).status_code == 200
    assert (await _apple(client)).status_code == 200
    # Заголовок H — не UUID: такой номер сервер не пишет, событие без устройства
    assert await _counted("login") == [("apple:new", None), ("apple:back", None)]


async def test_статистика_двойное_нажатие_один_новый_аккаунт(client, net):
    import asyncio

    await _forget_events()
    await asyncio.gather(_tg(client), _tg(client))
    await asyncio.gather(_apple(client), _apple(client))
    keys = sorted(k for k, _ in await _counted("login"))
    assert keys == ["apple:back", "apple:new", "telegram:back", "telegram:new"]


async def test_статистика_неудачный_вход_не_считается(client, net):
    await _forget_events()
    assert (await _tg(client, tg_token(key=STRANGER))).status_code == 401
    assert (await _apple(client, apple_token(key=STRANGER))).status_code == 401
    assert await _counted("login") == []


async def test_статистика_отладочная_сборка_не_считается(client, net):
    await _forget_events()
    debug = {"X-Device-Id": DEVICE, "X-Sayr-App": "ios/1.9.5-debug ru 26"}
    resp = await client.post("/api/v1/auth/telegram", json={"id_token": tg_token()}, headers=debug)
    assert resp.status_code == 200
    assert await _counted("login") == []


async def test_статистика_сбой_записи_не_ломает_вход(client, net, monkeypatch):
    """Событие пишется своей сессией после ответа: упала запись — вход есть"""
    from app import stats

    class Broken:
        async def __aenter__(self):
            raise RuntimeError("база статистики недоступна")

        async def __aexit__(self, *exc):
            return False

    await _forget_events()
    monkeypatch.setattr(stats, "SessionLocal", lambda: Broken())
    resp = await _tg(client)
    assert resp.status_code == 200 and resp.json()["token"]
    monkeypatch.undo()
    assert await _counted("login") == []


async def test_статистика_привязки_telegram(client, net):
    """ok — привязали на деле; повтор той же привязки нового не добавляет;
    taken — Telegram у другого аккаунта"""
    await _forget_events()
    h, _ = await _user(phone="+998935550000")
    body = {"id_token": tg_token(phone_number=None)}
    assert (await client.post("/api/v1/me/telegram", json=body, headers=h)).status_code == 200
    assert (await client.post("/api/v1/me/telegram", json=body, headers=h)).status_code == 200
    other, _ = await _user(apple_sub="apple-stats")
    taken = await client.post("/api/v1/me/telegram", json=body, headers=other)
    assert taken.status_code == 409
    assert [k for k, _ in await _counted("tg_link")] == ["ok", "taken"]


async def test_статистика_привязки_занятой_параллельно(client, net, monkeypatch):
    """Занятость, обнаруженная уникальностью при записи, — тоже taken"""
    from app.api import auth_providers

    await _forget_events()
    await _user(telegram_id=TG_ID)
    h, _ = await _user(apple_sub="apple-race-stats")
    real, calls = auth_providers._one, []

    async def blind_first(session, *where, **kw):
        calls.append(where)
        return None if len(calls) == 1 else await real(session, *where, **kw)

    monkeypatch.setattr(auth_providers, "_one", blind_first)
    resp = await client.post("/api/v1/me/telegram", json={"id_token": tg_token()}, headers=h)
    assert resp.status_code == 409
    assert [k for k, _ in await _counted("tg_link")] == ["taken"]


async def test_статистика_анкеты_первое_сохранение_и_правки(client, net):
    """first — сохранение, которое заполнило анкету; дальше — edit. Анкета,
    дополненная из Telegram при входе, ещё не заполнена человеком"""
    await _forget_events()
    token = (await _tg(client)).json()["token"]
    h = {"Authorization": f"Bearer {token}"} | HD
    for year in (1990, 1991):
        resp = await client.patch(
            "/api/v1/me", json={"first_name": "Abdulaziz", "birth_year": year}, headers=h
        )
        assert resp.status_code == 200, resp.text
    # Пустое тело ничего не сохраняет, отказ — тоже
    assert (await client.patch("/api/v1/me", json={}, headers=h)).status_code == 200
    bad = await client.patch("/api/v1/me", json={"first_name": "Ali2"}, headers=h)
    assert bad.status_code == 422
    assert await _counted("profile_save") == [("first", DEVICE), ("edit", DEVICE)]
