"""Вход по номеру: заявка, код, сессия, лимиты.

Канал подставной: в сеть не ходим и денег не тратим. Проверяем ровно то,
что делает сервер, — код он не придумывает и не хранит, только ведёт заявку.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text

from app.api import auth as auth_api
from app.auth.gateway import (
    CODE_EXPIRED,
    CODE_INVALID,
    CODE_UNKNOWN,
    CODE_VALID,
    SEND_FAILED,
    SEND_NO_TELEGRAM,
    SEND_OK,
    SendResult,
)
from app.db import SessionLocal, engine
from app.main import app
from app.models import ApiEvent, LoginRequest, User, UserSession


class FakeChannel:
    """Канал, который всё помнит и ничего не отправляет."""

    name = "telegram"

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.send_status = SEND_OK
        self.check_status = CODE_VALID
        #: Держит проверки, пока до неё не дойдут все участники гонки
        self.barrier: asyncio.Barrier | None = None
        #: Держит проверку, пока тест его не откроет: медленный шлюз
        self.gate: asyncio.Event | None = None
        self.checks = 0

    async def send(self, phone, *, ttl_sec, code_length, callback_url=None):
        self.sent.append(phone)
        if self.send_status != SEND_OK:
            return SendResult(status=self.send_status, error="TEST")
        return SendResult(status=SEND_OK, request_id=f"gw-{len(self.sent)}")

    async def check(self, request_id: str, code: str) -> str:
        self.checks += 1
        if self.barrier is not None:
            await self.barrier.wait()
        if self.gate is not None:
            await self.gate.wait()
        return self.check_status


@pytest.fixture
def channel():
    fake = FakeChannel()
    app.dependency_overrides[auth_api.get_channel] = lambda: fake
    yield fake
    app.dependency_overrides.pop(auth_api.get_channel, None)


@pytest.fixture(autouse=True)
async def clean():
    async with SessionLocal() as session:
        await session.execute(delete(UserSession))
        await session.execute(delete(User))
        await session.execute(delete(LoginRequest))
        await session.execute(delete(ApiEvent))
        await session.commit()


async def _request(client, phone="90 123 45 67", device="test-device"):
    return await client.post(
        "/api/v1/auth/request",
        json={"phone": phone},
        headers={"X-Device-Id": device, "X-Sayr-App": "ios/1.8.0"},
    )


def _from(ip: str) -> AsyncClient:
    """Клиент с другого адреса: лимит адреса считается по нему"""
    transport = ASGITransport(app=app, client=(ip, 5555))
    return AsyncClient(transport=transport, base_url="http://test")


async def _requests_in_base() -> int:
    async with SessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(LoginRequest))).scalar_one()


async def test_заявка_заводится_и_кода_в_ней_нет(client, channel):
    resp = await _request(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["channel"] == "telegram"
    assert body["resend_after"] > 0
    # Номер нормализован до международного вида
    assert channel.sent == ["+998901234567"]

    async with SessionLocal() as session:
        row = await session.get(LoginRequest, body["request_id"])
    assert row is not None
    assert row.phone == "+998901234567"
    assert row.gateway_request_id == "gw-1"
    assert row.status == "sent"
    # В строке заявки нет ни одного поля под код
    assert not hasattr(row, "code")


async def test_верный_код_заводит_человека_и_сессию(client, channel):
    request_id = (await _request(client)).json()["request_id"]
    resp = await client.post(
        "/api/v1/auth/verify",
        json={"request_id": request_id, "code": "123456"},
        headers={"X-Device-Id": "test-device", "X-Sayr-App": "ios/1.8.0"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"]
    assert body["user"]["phone"] == "+998901234567"
    assert body["user"]["profile_filled"] is False

    async with SessionLocal() as session:
        sessions = (await session.execute(select(UserSession))).scalars().all()
    assert len(sessions) == 1
    assert sessions[0].device_id == "test-device"
    assert sessions[0].platform == "ios"
    # Сам токен в базе не лежит, только отпечаток
    assert body["token"] not in sessions[0].token_hash


async def test_повторный_вход_находит_того_же_человека(client, channel):
    first = (await _request(client)).json()["request_id"]
    a = await client.post("/api/v1/auth/verify", json={"request_id": first, "code": "111111"})
    second = (await _request(client, device="second-phone")).json()["request_id"]
    b = await client.post("/api/v1/auth/verify", json={"request_id": second, "code": "222222"})

    assert a.json()["user"]["id"] == b.json()["user"]["id"]
    assert a.json()["token"] != b.json()["token"]
    async with SessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
        sessions = (await session.execute(select(UserSession))).scalars().all()
    assert len(users) == 1
    assert len(sessions) == 2


async def test_три_неверных_кода_сжигают_заявку(client, channel):
    request_id = (await _request(client)).json()["request_id"]
    channel.check_status = CODE_INVALID

    left = []
    for _ in range(3):
        resp = await client.post(
            "/api/v1/auth/verify", json={"request_id": request_id, "code": "000000"}
        )
        left.append(resp.json()["detail"]["attempts_left"])
    assert left == [2, 1, 0]

    # Даже верный код после этого не пускает
    channel.check_status = CODE_VALID
    resp = await client.post(
        "/api/v1/auth/verify", json={"request_id": request_id, "code": "123456"}
    )
    assert resp.status_code == 410
    async with SessionLocal() as session:
        assert (await session.execute(select(UserSession))).first() is None


async def test_истёкшая_заявка_не_принимает_верный_код(client, channel):
    request_id = (await _request(client)).json()["request_id"]
    async with SessionLocal() as session:
        row = await session.get(LoginRequest, request_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    resp = await client.post(
        "/api/v1/auth/verify", json={"request_id": request_id, "code": "123456"}
    )
    assert resp.status_code == 410
    assert resp.json()["detail"] == "code_expired"


async def test_заявка_со_стёртым_номером_никого_не_впускает(client, channel):
    """Аккаунт удалили, пока код был в пути: номер из заявки стёрт,
    и верный код не должен завести человека без номера"""
    request_id = (await _request(client)).json()["request_id"]
    async with SessionLocal() as session:
        row = await session.get(LoginRequest, request_id)
        row.phone = ""
        await session.commit()

    resp = await client.post(
        "/api/v1/auth/verify", json={"request_id": request_id, "code": "123456"}
    )
    assert resp.status_code == 410
    async with SessionLocal() as session:
        assert (await session.execute(select(User))).first() is None


async def test_непонятный_ответ_канала_не_сжигает_попытку(client, channel):
    request_id = (await _request(client)).json()["request_id"]
    channel.check_status = CODE_UNKNOWN
    resp = await client.post(
        "/api/v1/auth/verify", json={"request_id": request_id, "code": "123456"}
    )
    assert resp.status_code == 502
    async with SessionLocal() as session:
        row = await session.get(LoginRequest, request_id)
    assert row.attempts == 0
    assert row.status == "sent"


async def test_номер_без_телеграма_отвечает_отдельно(client, channel):
    channel.send_status = SEND_NO_TELEGRAM
    resp = await _request(client)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "no_telegram"
    # Заявка остаётся только для лимитов адреса и устройства: по ней
    # не войти, и номера у шлюза за ней нет
    async with SessionLocal() as session:
        row = (await session.execute(select(LoginRequest))).scalar_one()
    assert row.status == "unsent"
    assert row.gateway_request_id is None


async def test_отказ_канала_не_выдаёт_себя_за_отсутствие_телеграма(client, channel):
    channel.send_status = SEND_FAILED
    resp = await _request(client)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "channel_failed"


async def test_четвёртый_запрос_в_час_отбивается(client, channel):
    for _ in range(3):
        assert (await _request(client)).status_code == 200
    resp = await _request(client)
    assert resp.status_code == 429
    assert len(channel.sent) == 3


async def test_отбитый_запрос_не_оставляет_следа(client, channel):
    """Лимиты считаются по заявкам в базе: запрос, получивший 429,
    ничего не пишет — иначе скрипт раздувал бы счётчики, ничего не получая"""
    for _ in range(3):
        assert (await _request(client)).status_code == 200
    for n in range(20):
        assert (await _request(client, device=f"script-{n}")).status_code == 429
    assert await _requests_in_base() == 3


async def test_исчерпанный_адрес_не_сжигает_чужой_номер(client, channel):
    """Скрипт с адреса, упёршегося в лимит, долбит чужой номер: номер
    от этого не должен попасть в лимит и остаться без входа на сутки"""
    async with _from("203.0.113.7") as script:
        for n in range(20):
            resp = await _request(script, phone=f"+9989100000{n:02d}", device=f"script-{n}")
            assert resp.status_code == 200, resp.text
        for n in range(12):
            resp = await _request(script, phone="+998907654321", device=f"victim-{n}")
            assert resp.status_code == 429
    assert "+998907654321" not in channel.sent

    # Хозяин номера со своего телефона и своего адреса входит как обычно
    resp = await _request(client, phone="+998907654321", device="own-phone")
    assert resp.status_code == 200, resp.text


async def test_исчерпанное_устройство_не_сжигает_номер(client, channel):
    for n in range(5):
        resp = await _request(client, phone=f"+9989100000{n:02d}", device="script")
        assert resp.status_code == 200, resp.text
    for _ in range(5):
        assert (await _request(client, phone="+998907654321", device="script")).status_code == 429
    resp = await _request(client, phone="+998907654321", device="own-phone")
    assert resp.status_code == 200, resp.text


async def test_неотправленный_код_не_засчитывается_номеру(client, channel):
    """Шлюз не отправил — номер не виноват: квота номера считает только
    ушедшие коды, иначе три сбоя шлюза запирали бы человека на час"""
    channel.send_status = SEND_FAILED
    for _ in range(3):
        assert (await _request(client)).status_code == 502
    channel.send_status = SEND_OK
    resp = await _request(client)
    assert resp.status_code == 200, resp.text


async def test_ipv6_считается_по_сети_64(client, channel):
    """У одного провайдера IPv6 выдаётся сетью /64: адрес внутри неё
    меняется бесплатно, поэтому лимит держится за сеть, а не за адрес"""
    for n in range(20):
        async with _from(f"2001:db8:5:6::{n + 1:x}") as script:
            resp = await _request(script, phone=f"+9989100000{n:02d}", device=f"script-{n}")
            assert resp.status_code == 200, resp.text
    async with _from("2001:db8:5:6:ffff:ffff:ffff:ffff") as script:
        assert (await _request(script, phone="+998907654321", device="next")).status_code == 429
    async with _from("2001:db8:5:7::1") as neighbour:
        resp = await _request(neighbour, phone="+998907654321", device="neighbour")
        assert resp.status_code == 200, resp.text


async def test_пачка_параллельных_запросов_не_обходит_лимит(client, channel):
    """Лимит в базе без замка пропустил бы всю пачку: пока ни одна
    заявка не записана, каждый запрос видит пустой счётчик"""
    responses = await asyncio.gather(
        *(_request(client, device=f"burst-{n}") for n in range(10))
    )
    assert sorted(r.status_code for r in responses) == [200] * 3 + [429] * 7
    assert len(channel.sent) == 3


async def test_занятый_замок_лимитов_не_держит_запрос(client, channel, monkeypatch):
    """Под замком лимитов пара счётов и одна запись. Если он занят дольше,
    база занята чем-то другим: ждать без конца значит держать соединение
    из пула, и за входом вставал бы весь API"""
    monkeypatch.setattr(auth_api, "_LOCK_WAIT", "100ms")
    async with SessionLocal() as holder:
        await holder.execute(select(func.pg_advisory_xact_lock(auth_api._LIMITS_LOCK, 0)))
        resp = await asyncio.wait_for(_request(client), 5)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "busy"
    assert channel.sent == []
    assert await _requests_in_base() == 0
    # Замок свободен — вход как обычно
    assert (await _request(client)).status_code == 200


async def test_суточный_потолок_кодов_бережёт_счёт_шлюза(client, channel, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "login_codes_per_day", 2)
    assert (await _request(client, phone="+998901000001", device="a")).status_code == 200
    assert (await _request(client, phone="+998901000002", device="b")).status_code == 200
    resp = await _request(client, phone="+998901000003", device="c")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "daily_cap"
    assert len(channel.sent) == 2

    # Код проверяющего не платный — потолок его не держит
    monkeypatch.setattr(settings, "login_test_phone", "+998900000000")
    monkeypatch.setattr(settings, "login_test_code", "424242")
    assert (await _request(client, phone="+998900000000", device="d")).status_code == 200


async def test_суточный_потолок_обращений_держит_номера_без_телеграма(
    client, channel, monkeypatch
):
    """Номер без телеграма платного кода не тратит, но шлюз о нём спрашивают,
    и заявка ложится в таблицу: скрипт со случайными номерами со многих
    адресов раздувал бы её без меры, а с ней и цену каждого счёта лимита"""
    from app.config import settings

    monkeypatch.setattr(settings, "login_gateway_calls_per_day", 3)
    channel.send_status = SEND_NO_TELEGRAM
    for n in range(3):
        resp = await _request(client, phone=f"+9989100000{n:02d}", device=f"script-{n}")
        assert resp.status_code == 409
    resp = await _request(client, phone="+998910000099", device="script-99")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "daily_cap"
    assert len(channel.sent) == 3
    assert await _requests_in_base() == 3


async def test_потолок_пишет_в_журнал_раз_в_час(client, channel, monkeypatch, caplog):
    """Отбитые потолком запросы в базу ничего не пишут и могут идти сплошным
    потоком — журнал не должен тонуть в одинаковых строках"""
    from app.config import settings

    monkeypatch.setattr(settings, "login_codes_per_day", 1)
    monkeypatch.setattr(auth_api, "_cap_warned_at", float("-inf"))
    assert (await _request(client, device="first")).status_code == 200
    with caplog.at_level(logging.WARNING, logger=auth_api.log.name):
        for n in range(5):
            resp = await _request(client, phone=f"+99890100000{n}", device=f"next-{n}")
            assert resp.status_code == 503
    assert len([r for r in caplog.records if "потолок" in r.getMessage()]) == 1


async def test_счёты_лимитов_читают_только_своё_окно():
    """Цена проверки лимитов не должна расти с числом заявок: каждый счёт —
    отрезок индекса по ключу и времени, а платный потолок — по частичному
    индексу без неотправленных заявок, которых скрипт наплодит больше всех.
    План берём общий, как у закешированного запроса: с условием потолка
    в параметрах частичный индекс ему недоступен, и счёт читал бы все
    заявки за сутки — под общим замком входа"""
    sent = LoginRequest.status != "unsent"
    counts = {
        "ix_login_requests_ip_created_at": auth_api._counted(
            3600, LoginRequest.ip == "203.0.113.7"
        ),
        "ix_login_requests_device_id_created_at": auth_api._counted(
            3600, LoginRequest.device_id == "script"
        ),
        "ix_login_requests_phone_created_at": auth_api._counted(
            86_400, (LoginRequest.phone == "+998901234567") & sent
        ),
        "ix_login_requests_codes_created_at": auth_api._counted(86_400, auth_api._PAID),
    }
    # Та же база и тот же драйвер, только параметры $1, $2 — как их видит PREPARE
    dialect = type(engine.dialect)(paramstyle="numeric_dollar")
    async with SessionLocal() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        await session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        for index, query in counts.items():
            compiled = query.compile(dialect=dialect)
            values = [compiled.params[name] for name in compiled.positiontup]
            types = ", ".join(
                "interval" if isinstance(value, timedelta) else "text" for value in values
            )
            args = ", ".join(
                f"'{int(value.total_seconds())} seconds'"
                if isinstance(value, timedelta)
                else f"'{value}'"
                for value in values
            )
            await session.execute(text(f"PREPARE limit_count({types}) AS {compiled}"))
            try:
                plan = (
                    await session.execute(text(f"EXPLAIN EXECUTE limit_count({args})"))
                ).scalars().all()
            finally:
                await session.execute(text("DEALLOCATE limit_count"))
            assert index in "\n".join(plan), plan
            assert any("Index Cond" in line and "created_at" in line for line in plan), plan


async def test_кривой_номер_не_доходит_до_канала(client, channel):
    resp = await _request(client, phone="12")
    assert resp.status_code == 422
    assert channel.sent == []


async def test_без_токена_шлюза_вход_выключен(client):
    # Без подставного канала настройка пуста, и ручка честно об этом говорит
    resp = await _request(client)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "channel_off"


async def test_выход_гасит_только_своё_устройство(client, channel):
    first = (await _request(client)).json()["request_id"]
    one = (await client.post(
        "/api/v1/auth/verify", json={"request_id": first, "code": "111111"}
    )).json()["token"]
    second = (await _request(client, device="second-phone")).json()["request_id"]
    two = (await client.post(
        "/api/v1/auth/verify", json={"request_id": second, "code": "222222"}
    )).json()["token"]

    out = await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {one}"})
    assert out.status_code == 204
    # Второй токен продолжает работать: гасится одна сессия, а не все
    again = await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {two}"})
    assert again.status_code == 204
    # Погашенный токен больше не принимается
    dead = await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {one}"})
    assert dead.status_code == 401


async def test_отчёт_о_доставке_выключен_без_ключа(client, channel):
    request_id = (await _request(client)).json()["request_id"]
    resp = await client.post(
        "/api/v1/auth/callback/telegram",
        json={"request_id": "gw-1", "delivery_status": {"status": "expired"}},
    )
    assert resp.status_code == 404
    async with SessionLocal() as session:
        row = await session.get(LoginRequest, request_id)
    assert row.status == "sent"


async def test_отчёт_о_доставке_закрывает_недоставленную_заявку(client, channel, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "tg_gateway_callback_key", "secret")
    request_id = (await _request(client)).json()["request_id"]
    resp = await client.post(
        "/api/v1/auth/callback/telegram?key=secret",
        json={"request_id": "gw-1", "delivery_status": {"status": "expired"}},
    )
    assert resp.status_code == 204
    async with SessionLocal() as session:
        row = await session.get(LoginRequest, request_id)
    assert row.status == "failed"

    # И код по ней уже не принимается
    resp = await client.post(
        "/api/v1/auth/verify", json={"request_id": request_id, "code": "123456"}
    )
    assert resp.status_code == 410


async def test_ошибка_проверки_кода(client, channel):
    request_id = (await _request(client)).json()["request_id"]
    channel.check_status = CODE_EXPIRED
    resp = await client.post(
        "/api/v1/auth/verify", json={"request_id": request_id, "code": "123456"}
    )
    assert resp.status_code == 410
    assert resp.json()["detail"] == "code_expired"


async def test_тестовый_номер_проверяющего_входит_без_канала(client, monkeypatch):
    """Проверяющим App Store и Play код в телеграм не приходит: номер из
    настроек заводит заявку без канала, а код сравнивается с настройкой"""
    from app.config import settings

    monkeypatch.setattr(settings, "login_test_phone", "+998900000000")
    monkeypatch.setattr(settings, "login_test_code", "424242")
    r = await _request(client, phone="+998900000000")
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    bad = await client.post("/api/v1/auth/verify", json={"request_id": rid, "code": "000000"})
    assert bad.status_code == 400
    ok = await client.post("/api/v1/auth/verify", json={"request_id": rid, "code": "424242"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["phone"] == "+998900000000"


async def test_без_настройки_тестового_номера_обхода_нет(client):
    # Канала нет и обход не задан — 503, как для всех
    r = await _request(client, phone="+998900000000")
    assert r.status_code == 503


async def test_параллельные_догадки_не_превышают_попыток(client, monkeypatch):
    """Код проверяющего сверяется у нас, а не у шлюза: без блокировки
    строки десять параллельных догадок проверились бы все десять"""
    from app.config import settings

    monkeypatch.setattr(settings, "login_test_phone", "+998900000000")
    monkeypatch.setattr(settings, "login_test_code", "424242")
    rid = (await _request(client, phone="+998900000000")).json()["request_id"]

    responses = await asyncio.gather(
        *(
            client.post("/api/v1/auth/verify", json={"request_id": rid, "code": f"00000{n}"})
            for n in range(10)
        )
    )
    # Догадки, заставшие заявку занятой, отбиваются сразу; добиваем по одной
    for n in range(3):
        responses.append(
            await client.post("/api/v1/auth/verify", json={"request_id": rid, "code": f"11111{n}"})
        )
    assert {r.status_code for r in responses} <= {400, 410, 429}
    # Проверенная догадка — 400 или 410 с числом попыток; сгоревшая заявка
    # отвечает 410 без него, до сверки кода
    checked = [
        r
        for r in responses
        if r.status_code == 400 or (r.status_code == 410 and isinstance(r.json()["detail"], dict))
    ]
    assert len(checked) == 3
    async with SessionLocal() as session:
        row = await session.get(LoginRequest, rid)
    assert row.attempts == 3
    assert row.status == "expired"
    ok = await client.post("/api/v1/auth/verify", json={"request_id": rid, "code": "424242"})
    assert ok.status_code == 410


async def test_проверка_занятой_заявки_не_ждёт_в_очереди(client, channel):
    """Пока шлюз проверяет код (до 10 с), заявка под замком. Вторая проверка
    той же заявки не встаёт за ней в очередь с соединением из пула, а сразу
    отбивается: иначе десяток догадок к одной заявке занимал бы весь пул"""
    rid = (await _request(client)).json()["request_id"]
    channel.gate = asyncio.Event()
    first = asyncio.create_task(
        client.post("/api/v1/auth/verify", json={"request_id": rid, "code": "111111"})
    )
    try:
        for _ in range(500):
            if channel.checks:
                break
            await asyncio.sleep(0.01)
        assert channel.checks == 1
        second = await asyncio.wait_for(
            client.post("/api/v1/auth/verify", json={"request_id": rid, "code": "222222"}), 5
        )
    finally:
        channel.gate.set()
    assert second.status_code == 429
    assert second.json()["detail"] == "too_often"
    assert (await first).status_code == 200
    assert channel.checks == 1


async def test_двойное_нажатие_на_новом_номере_не_роняет_вход(client, channel):
    """Два подтверждения для нового номера разом: оба заводят человека,
    и второе не должно падать на уникальности номера"""
    first = (await _request(client, device="one")).json()["request_id"]
    second = (await _request(client, device="two")).json()["request_id"]
    channel.barrier = asyncio.Barrier(2)
    a, b = await asyncio.gather(
        client.post("/api/v1/auth/verify", json={"request_id": first, "code": "111111"}),
        client.post("/api/v1/auth/verify", json={"request_id": second, "code": "111111"}),
    )
    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text
    assert a.json()["user"]["id"] == b.json()["user"]["id"]
    async with SessionLocal() as session:
        assert len((await session.execute(select(User))).scalars().all()) == 1
        # Новым аккаунт посчитала одна: вставка второй упёрлась в первую
        logins = (
            await session.execute(select(ApiEvent.slug).where(ApiEvent.kind == "login"))
        ).scalars().all()
    assert sorted(logins) == ["phone:back", "phone:new"]


# --- Статистика входа (02.10.2026) ------------------------------------------

DEVICE = "3c9e1a7b-5d2f-4b8e-a6c1-9f0d2e4b7a35"


async def _logins() -> list[tuple[str | None, str | None]]:
    async with SessionLocal() as session:
        rows = await session.execute(
            select(ApiEvent.slug, ApiEvent.device)
            .where(ApiEvent.kind == "login")
            .order_by(ApiEvent.id)
        )
        return [tuple(r) for r in rows.all()]


async def test_статистика_вход_по_номеру_новый_и_вернувшийся(client, channel):
    headers = {"X-Device-Id": DEVICE, "X-Sayr-App": "android/1.8.0 uz 15"}
    for _ in range(2):
        request_id = (await _request(client, device=DEVICE)).json()["request_id"]
        resp = await client.post(
            "/api/v1/auth/verify", json={"request_id": request_id, "code": "123456"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
    assert await _logins() == [("phone:new", DEVICE), ("phone:back", DEVICE)]


async def test_статистика_неверный_код_не_вход(client, channel):
    request_id = (await _request(client)).json()["request_id"]
    channel.check_status = CODE_INVALID
    resp = await client.post("/api/v1/auth/verify", json={"request_id": request_id, "code": "000000"})
    assert resp.status_code == 400
    assert await _logins() == []
