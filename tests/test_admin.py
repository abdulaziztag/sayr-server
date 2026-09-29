"""Админка: вход по паролю, счётчик промахов, срок сессии, выгрузки, плитка снимков.

Счётчик промахов (app/login_guard.py) общий для /admin и /seasons/review,
поэтому обе двери проверяются здесь, рядом. Удаление человека из админки —
в test_rooms.py: там всё, чтобы завести ему комнаты.
"""

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from asgi_lifespan import LifespanManager
from fastapi_storages import StorageFile
from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner
from sqlalchemy import delete, select
from starlette.requests import Request

from app import login_guard
from app.config import Settings, settings
from app.db import SessionLocal
from app.main import app
from app.models import Difficulty, Place, PlaceCategory, PlacePhoto, Region, photo_storage


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Свои часы для счётчика — чтобы не ждать паузу по-настоящему.

    Подменяем модуль time только внутри login_guard: общий time.monotonic
    трогать нельзя, на нём живёт цикл событий. Сам счётчик чистит
    conftest.forget_login_failures — перед каждым тестом всего набора
    """
    now = [1_000.0]
    monkeypatch.setattr(login_guard, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


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


@pytest.mark.parametrize(
    ("url", "wrong"), [("/admin/login", 400), ("/seasons/review/login", 401)]
)
async def test_залп_попыток_разом_упирается_в_ту_же_паузу(url, wrong):
    """В /admin/login между проверкой паузы и записью промаха читалась
    форма — await, на котором цикл отдаёт ход. Залп параллельных запросов
    весь проходил проверку раньше, чем хоть один успевал записать промах:
    сотня попыток разом — сотня проверенных паролей вместо шести"""
    async with from_ip("203.0.113.40") as c:
        answers = await asyncio.gather(*(
            c.post(url, data={"username": settings.admin_username, "password": "мимо"})
            for _ in range(login_guard.FREE_FAILURES * 5)
        ))
    codes = [a.status_code for a in answers]
    assert codes.count(wrong) == login_guard.FREE_FAILURES + 1, codes
    assert codes.count(429) == len(codes) - login_guard.FREE_FAILURES - 1


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


def test_счётчик_помнит_не_больше_предела_и_забывает_давних(clock, monkeypatch):
    """У кого своя /48, тот заводит по новой /64 на каждую попытку: словарь
    рос бы без края, а каждая попытка входа перебирала бы его целиком.
    Вытесняется и забывается тот, кто промахивался давнее всех"""
    monkeypatch.setattr(login_guard, "MAX_SLOTS", 3)
    net = [_request(f"2001:db8:0:{n}::1") for n in range(4)]

    def kept():
        return [n for n, who in enumerate(net)
                if login_guard._slot(login_guard.ADMIN, who) in login_guard._FAILS]

    for who in net[:3]:
        login_guard.failed(login_guard.ADMIN, who)
        clock[0] += 1
    # Повторный промах поднимает сеть к свежим — вытеснят не её
    login_guard.failed(login_guard.ADMIN, net[0])
    login_guard.failed(login_guard.ADMIN, net[3])
    assert kept() == [0, 2, 3]

    # Сутки спустя забыты все, кто молчал, а промахнувшийся под конец — нет
    clock[0] += login_guard.FORGET_SEC - 10
    login_guard.failed(login_guard.ADMIN, net[2])
    clock[0] += 20
    login_guard.locked_for(login_guard.ADMIN, net[1])
    assert kept() == [2]


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


# --- Плитка снимков ---------------------------------------------------------


class _Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.forms.append(dict(attrs))


async def test_апостроф_в_названии_не_ломает_вопрос_перед_удалением(admin_client):
    """Название подставлялось внутрь confirm('…'): браузер снимает
    HTML-экранирование с атрибута до запуска скрипта, и апостроф ломал
    обработчик — снимок удалялся без вопроса, а подобранное название
    выполнилось бы в админке как код"""
    name = "Ko'l'); alert(1); ('"
    async with SessionLocal() as session:
        region = (await session.execute(select(Region).limit(1))).scalar_one()
        place = Place(
            slug="test-apostrophe", name=name, region_id=region.id,
            category=PlaceCategory.lake, difficulty=Difficulty.easy,
            lat=41.0, lng=70.0, short_desc="",
        )
        session.add(place)
        await session.flush()
        session.add(PlacePhoto(place_id=place.id, sort_order=0,
                               file=StorageFile(name="apostrophe.jpg", storage=photo_storage)))
        await session.commit()
        place_id = place.id
    try:
        page = await admin_client.get(f"/admin/place/details/{place_id}")
        assert page.status_code == 200
        parser = _Forms()
        parser.feed(page.text)
        deleting = [f for f in parser.forms if f.get("action", "").endswith("/photo-delete")]
        assert len(deleting) == 1
        form = deleting[0]
        assert form["onsubmit"] == "return confirm(this.dataset.confirm)"
        assert form["data-confirm"] == f"Убрать этот снимок у места «{name}»?"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Place).where(Place.id == place_id))
            await session.commit()
