"""Приём событий с телефона: POST /api/v1/events."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.api.events import DAILY_CAP, clamp_at, validate_key
from app.db import SessionLocal
from app.models import ApiEvent, Device

HEADERS = {"X-Device-Id": "dev-events", "X-Sayr-App": "android/1.7.0 ru 14"}


async def _clear():
    async with SessionLocal() as session:
        await session.execute(delete(ApiEvent))
        await session.execute(delete(Device))
        await session.commit()


async def _rows(**where):
    async with SessionLocal() as session:
        query = select(ApiEvent).order_by(ApiEvent.id)
        if where.get("kind"):
            query = query.where(ApiEvent.kind == where["kind"])
        return (await session.execute(query)).scalars().all()


def _event(id: str, kind: str, key: str | None = None, at: str | None = None) -> dict:
    return {
        "id": id,
        "kind": kind,
        "key": key,
        "at": at or datetime.now(timezone.utc).isoformat(),
    }


async def _post(client, events, headers=HEADERS):
    return await client.post("/api/v1/events", json={"events": events}, headers=headers)


async def test_batch_is_written_with_keys(client):
    await _clear()
    resp = await _post(client, [
        _event("e1000001", "nav_start", "test-peak"),
        _event("e1000002", "sticker_layout", "3"),
        _event("e1000003", "app_open"),
    ])
    assert resp.status_code == 204
    rows = await _rows()
    assert [(r.kind, r.slug, r.device) for r in rows] == [
        ("nav_start", "test-peak", "dev-events"),
        ("sticker_layout", "3", "dev-events"),
        ("app_open", None, "dev-events"),
    ]
    assert all(r.client_id for r in rows)


async def test_repeated_batch_does_not_double(client):
    """Пачка ушла, подтверждение потерялось, клиент повторил — событий столько же."""
    await _clear()
    batch = [_event("e2000001", "nav_finish", "test-peak"), _event("e2000002", "rec_finish")]
    await _post(client, batch)
    await _post(client, batch)
    assert len(await _rows()) == 2


async def test_bad_events_are_dropped_but_neighbours_live(client):
    await _clear()
    await _post(client, [
        _event("e3000001", "teleport", "test-peak"),          # чужой вид
        _event("e3000002", "nav_start", "Bad Slug!"),         # кривой ключ
        _event("e3000003", "app_open", "with-key"),           # ключ там, где его нет
        _event("e3000004", "store_ios", "gorets"),            # клик в магазин — только с сервера
        _event("e3000005", "favorite", "test-lake"),          # живой
        _event("bad id", "favorite", "test-lake"),            # кривой номер
    ])
    rows = await _rows()
    assert [(r.kind, r.slug) for r in rows] == [("favorite", "test-lake")]


async def test_optional_key_kinds_accept_empty(client):
    await _clear()
    await _post(client, [
        _event("e4000001", "rec_finish"),
        _event("e4000002", "rec_finish", "test-peak"),
        _event("e4000003", "nav_finish"),   # у навигации ключ обязателен
    ])
    rows = await _rows()
    assert [(r.kind, r.slug) for r in rows] == [("rec_finish", None), ("rec_finish", "test-peak")]


async def test_search_is_lowercased_and_bounded():
    ok, key = validate_key("search", "  Большой ЧИМГАН ")
    assert ok and key == "большой чимган"
    ok, _ = validate_key("search", "x" * 41)
    assert not ok
    ok, _ = validate_key("search", "drop table; --")
    assert not ok


def test_clamp_at_keeps_day_within_window():
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    future = clamp_at("2026-09-25T00:00:00Z", now, 30)
    assert future == now
    ancient = clamp_at("2020-01-01T00:00:00Z", now, 30)
    assert ancient == now - timedelta(days=30)
    yesterday = clamp_at("2026-09-19T07:00:00Z", now, 30)
    assert yesterday == datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc)
    naive = clamp_at("2026-09-19T07:00:00", now, 30)
    assert naive == datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc)
    assert clamp_at("вчера", now, 30) is None


async def test_client_timestamp_becomes_event_time(client):
    await _clear()
    at = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0)
    await _post(client, [_event("e5000001", "nav_start", "test-peak", at.isoformat())])
    rows = await _rows()
    assert rows[0].ts == at


async def test_daily_cap_silently_stops_writing(client):
    await _clear()
    for batch in range(DAILY_CAP // 100):
        events = [_event(f"e6{batch:03d}{i:04d}", "app_open") for i in range(100)]
        await _post(client, events)
    assert len(await _rows()) == DAILY_CAP
    resp = await _post(client, [_event("e6-over-cap-1", "app_open")])
    assert resp.status_code == 204
    assert len(await _rows()) == DAILY_CAP


async def test_debug_build_is_not_counted(client):
    await _clear()
    await _post(client, [_event("e7000001", "app_open")],
                headers={"X-Device-Id": "dev-debug", "X-Sayr-App": "ios/1.7.1-debug ru 26"})
    assert await _rows() == []
    async with SessionLocal() as session:
        assert (await session.execute(select(Device))).scalars().all() == []


async def test_missing_device_header_writes_nothing(client):
    await _clear()
    resp = await _post(client, [_event("e8000001", "app_open")], headers={})
    assert resp.status_code == 204
    assert await _rows() == []


async def test_header_fills_device_once_per_day(client):
    await _clear()
    await _post(client, [_event("e9000001", "app_open")],
                headers={"X-Device-Id": "dev-hdr", "X-Sayr-App": "ios/1.7.1 uz 26"})
    await _post(client, [_event("e9000002", "app_open")],
                headers={"X-Device-Id": "dev-hdr", "X-Sayr-App": "ios/9.9.9 ru 27"})
    async with SessionLocal() as session:
        device = (await session.execute(select(Device))).scalar_one()
    assert device.platform == "ios"
    assert device.app_version == "1.7.1"          # второй заголовок за день не перезаписал
    assert device.lang == "uz"
    assert device.os_major == "26"
    assert device.last_seen == date.today()


async def test_oversized_batch_is_cut_to_limit_not_rejected(client):
    """Сто первое событие не даёт 422: иначе клиент слал бы ту же пачку вечно."""
    from app.api.events import BATCH_MAX

    await _clear()
    events = [_event(f"e10{i:06d}", "app_open") for i in range(BATCH_MAX + 50)]
    resp = await _post(client, events)
    assert resp.status_code == 204
    assert len(await _rows()) == BATCH_MAX

