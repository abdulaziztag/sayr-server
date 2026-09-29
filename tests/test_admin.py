"""Админка: вход по паролю, счётчик промахов, срок сессии, выгрузки.

Счётчик промахов (app/login_guard.py) общий для /admin и /seasons/review,
поэтому обе двери проверяются здесь, рядом. Удаление человека из админки —
в test_rooms.py: там всё, чтобы завести ему комнаты.
"""

import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner
from starlette.requests import Request

from app import login_guard
from app.config import Settings, settings
from app.main import app


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Свои часы для счётчика — чтобы не ждать паузу по-настоящему.

    Подменяем модуль time только внутри login_guard: общий time.monotonic
    трогать нельзя, на нём живёт цикл событий. Счётчик живёт в памяти
    процесса и переживал бы тест — чистим до и после
    """
    now = [1_000.0]
    monkeypatch.setattr(login_guard, "time", SimpleNamespace(monotonic=lambda: now[0]))
    login_guard._FAILS.clear()
    yield now
    login_guard._FAILS.clear()


@asynccontextmanager
async def from_ip(ip: str):
    """Клиент с чужого адреса: промахи 127.0.0.1 заперли бы вход admin_client"""
    async with LifespanManager(app):
        transport = ASGITransport(app=app, client=(ip, 40000))
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            yield c


async def _admin_login(c: AsyncClient, password: str):
    return await c.post(
        "/admin/login", data={"username": settings.admin_username, "password": password}
    )


def _request(ip: str) -> Request:
    return Request({"type": "http", "client": (ip, 1), "headers": []})


# --- Вход -------------------------------------------------------------------


async def test_кириллица_в_пароле_не_роняет_вход(monkeypatch):
    """compare_digest на строках с не-ASCII бросал TypeError: русская
    раскладка давала пятисотку, а кириллический пароль не подошёл бы никогда"""
    async with from_ip("198.51.100.10") as c:
        wrong = await _admin_login(c, "пароль")
        assert wrong.status_code == 400

        monkeypatch.setattr(settings, "admin_password", "горы-2026")
        entered = await _admin_login(c, "горы-2026")
        assert entered.status_code == 302
        assert (await c.get("/admin/place/list")).status_code == 200


async def test_промахи_запирают_вход_с_адреса_и_пауза_проходит(clock):
    async with from_ip("203.0.113.7") as c:
        for _ in range(login_guard.FREE_FAILURES + 1):
            assert (await _admin_login(c, "мимо")).status_code == 400

        # Заперто — и верный пароль не проверяется: иначе перебор шёл бы дальше
        locked = await _admin_login(c, settings.admin_password)
        assert locked.status_code == 429
        assert "Попробуйте через 1 мин." in locked.text

        # Соседний адрес пауза не касается — владельца с другой сети не запереть
        async with from_ip("203.0.113.8") as other:
            assert (await _admin_login(other, settings.admin_password)).status_code == 302

        clock[0] += login_guard.FIRST_LOCK_SEC + 1
        assert (await _admin_login(c, settings.admin_password)).status_code == 302
        # Удачный вход прощает прежние промахи: следующий снова просто «не подошёл»
        assert (await _admin_login(c, "мимо")).status_code == 400


def test_пауза_растёт_вдвое_до_потолка(clock):
    who = _request("192.0.2.1")
    for _ in range(login_guard.FREE_FAILURES):
        login_guard.failed(login_guard.ADMIN, who)
        assert login_guard.locked_for(login_guard.ADMIN, who) == 0

    waits = []
    for _ in range(7):
        login_guard.failed(login_guard.ADMIN, who)
        waits.append(login_guard.locked_for(login_guard.ADMIN, who))
        clock[0] += waits[-1]
    assert waits == [60, 120, 240, 480, 900, 900, 900]

    # Сутки тишины — адрес забыт
    clock[0] += login_guard.FORGET_SEC + 1
    login_guard.locked_for(login_guard.ADMIN, who)
    assert login_guard._FAILS == {}


def test_ipv6_считается_сетью_64():
    """Абоненту выдают целую /64: по отдельным адресам перебор шёл бы
    с нового адреса на каждую попытку"""
    key = login_guard.client_key
    assert key(_request("2001:db8:1:2::5")) == key(_request("2001:db8:1:2:ffff::1"))
    assert key(_request("2001:db8:1:2::5")) != key(_request("2001:db8:1:3::5"))
    assert key(_request("::ffff:203.0.113.7")) == "203.0.113.7"
    assert key(_request("203.0.113.7")) == "203.0.113.7"


async def test_проверка_сезонов_без_своего_пароля_считает_промахи_в_дверь_админки():
    """Пока у проверки нет своего пароля, она пускает по админскому — перебор
    через неё тот же перебор админки и запирает обе формы"""
    async with from_ip("203.0.113.20") as c:
        for _ in range(login_guard.FREE_FAILURES + 1):
            wrong = await c.post("/seasons/review/login", data={"password": "мимо"})
            assert wrong.status_code == 401
        locked = await c.post("/seasons/review/login",
                              data={"password": settings.admin_password})
        assert locked.status_code == 429 and "Попробуйте через" in locked.text
        assert (await _admin_login(c, settings.admin_password)).status_code == 429


async def test_свой_пароль_проверки_считается_отдельно(monkeypatch):
    """Знающий пароль проверки — кто-то из клуба — не должен удачным входом
    сбрасывать промахи по админскому, и наоборот, перебор его пароля
    не запирает владельцу админку"""
    monkeypatch.setattr(settings, "review_password", "клуб")
    async with from_ip("203.0.113.30") as c:
        for _ in range(login_guard.FREE_FAILURES + 1):
            await c.post("/seasons/review/login", data={"password": "мимо"})
        assert (await c.post("/seasons/review/login",
                             data={"password": "клуб"})).status_code == 429
        assert (await _admin_login(c, settings.admin_password)).status_code == 302


# --- Сессия -----------------------------------------------------------------


async def test_сессия_админки_живёт_полдня():
    async with from_ip("198.51.100.20") as c:
        entered = await _admin_login(c, settings.admin_password)
    cookie = entered.headers["set-cookie"]
    assert f"Max-Age={settings.admin_session_max_age_sec}" in cookie
    # Не две недели, как у Starlette по умолчанию
    assert Settings.model_fields["admin_session_max_age_sec"].default == 12 * 3600


async def test_смена_пароля_выводит_всех_из_админки(admin_client, monkeypatch):
    assert (await admin_client.get("/admin/place/list")).status_code == 200
    monkeypatch.setattr(settings, "admin_password", "новый-пароль")
    out = await admin_client.get("/admin/place/list")
    assert out.status_code == 302 and out.headers["location"].endswith("/admin/login")


async def test_старая_сессия_без_отпечатка_не_пускает():
    """Сессии до этой правки хранили просто admin: true — их больше не принимаем"""
    forged = TimestampSigner(settings.secret_key).sign(
        base64.b64encode(json.dumps({"admin": True}).encode())
    ).decode()
    async with from_ip("198.51.100.30") as c:
        c.cookies.set("session", forged)
        assert (await c.get("/admin/place/list")).status_code == 302


# --- Выгрузки ---------------------------------------------------------------


@pytest.mark.parametrize(
    "identity", ["user", "push-token", "place-report", "room-report", "tg-message"]
)
async def test_выгрузки_с_личными_данными_закрыты(admin_client, identity):
    """Выгрузка идёт мимо маски номера: /admin/user/export/csv отдавала
    телефоны всех людей целиком"""
    assert (await admin_client.get(f"/admin/{identity}/export/csv")).status_code == 403
    assert (await admin_client.get(f"/admin/{identity}/export/json")).status_code == 403


async def test_заявки_тестировщиков_выгружаются(admin_client):
    """Намеренное исключение: адреса и так видны списком, а Play Console
    принимает тестировщиков файлом CSV"""
    assert (await admin_client.get("/admin/tester-signup/export/csv")).status_code == 200
