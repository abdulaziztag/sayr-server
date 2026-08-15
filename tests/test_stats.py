from datetime import date, datetime, timedelta

from sqlalchemy import delete, select

from app import stats
from app.db import SessionLocal
from app.models import ApiEvent, DailyStat, Device


async def _events(**where):
    async with SessionLocal() as session:
        query = select(ApiEvent)
        if where.get("kind"):
            query = query.where(ApiEvent.kind == where["kind"])
        return (await session.execute(query)).scalars().all()


async def _clear():
    async with SessionLocal() as session:
        await session.execute(delete(ApiEvent))
        await session.execute(delete(DailyStat))
        await session.execute(delete(Device))
        await session.commit()


async def test_catalog_open_recorded(client):
    await _clear()
    await client.get("/api/v1/places", headers={"X-Device-Id": "dev-catalog"})
    events = await _events(kind="catalog")
    assert len(events) == 1
    assert events[0].device == "dev-catalog"
    assert events[0].slug is None


async def test_place_open_keeps_slug(client):
    await _clear()
    await client.get("/api/v1/places/test-peak", headers={"X-Device-Id": "dev-place"})
    events = await _events(kind="place")
    assert [e.slug for e in events] == ["test-peak"]


async def test_weather_and_intents_are_not_place_opens(client):
    """Соседи /places/{slug} открытием места не считаются: матчим маршрут,
    а не префикс пути."""
    await _clear()
    await client.get("/api/v1/places/test-peak/intents")
    assert await _events(kind="place") == []


async def test_missing_header_writes_null_device(client):
    """Старый клиент без заголовка попадает в просмотры, но не в уникальные."""
    await _clear()
    await client.get("/api/v1/places")
    events = await _events(kind="catalog")
    assert len(events) == 1
    assert events[0].device is None
    async with SessionLocal() as session:
        assert (await session.execute(select(Device))).scalars().all() == []


async def test_failed_request_is_not_recorded(client):
    await _clear()
    resp = await client.get("/api/v1/places/no-such-place")
    assert resp.status_code == 404
    assert await _events() == []


async def test_first_seen_written_once(client):
    await _clear()
    await client.get("/api/v1/places", headers={"X-Device-Id": "dev-twice"})
    await client.get("/api/v1/places", headers={"X-Device-Id": "dev-twice"})
    async with SessionLocal() as session:
        devices = (await session.execute(select(Device))).scalars().all()
    assert [d.device for d in devices] == ["dev-twice"]
    assert devices[0].first_seen == date.today()


async def test_rotation_aggregates_and_prunes():
    """Закрытый день сворачивается в агрегат, сырьё старше срока стирается."""
    await _clear()
    today = date.today()
    yesterday = today - timedelta(days=1)
    long_ago = datetime.now().astimezone() - timedelta(days=400)

    async with SessionLocal() as session:
        session.add_all(
            [
                ApiEvent(kind="place", slug="a", device="d1", ts=_at(yesterday)),
                ApiEvent(kind="place", slug="b", device="d2", ts=_at(yesterday)),
                ApiEvent(kind="catalog", device="d1", ts=_at(yesterday)),
                ApiEvent(kind="gpx", slug="t.gpx", device="d1", ts=_at(yesterday)),
                ApiEvent(kind="place", slug="c", device="d3", ts=long_ago),
            ]
        )
        session.add(Device(device="d1", first_seen=yesterday))
        await session.commit()

        await stats.rotate(session, today=today)

        row = (
            await session.execute(select(DailyStat).where(DailyStat.day == yesterday))
        ).scalar_one()
        assert row.place_opens == 2
        assert row.catalog_opens == 1
        assert row.gpx_downloads == 1
        assert row.active_devices == 2
        assert row.new_devices == 1

        # Старое сырьё стёрто, вчерашнее — на месте: срок хранения 30 дней
        left = (await session.execute(select(ApiEvent.slug))).scalars().all()
        assert "c" not in left
        assert {"a", "b"} <= set(left)


def _at(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time()).astimezone() + timedelta(hours=12)


async def test_dashboard_survives_empty_tables():
    await _clear()
    async with SessionLocal() as session:
        data = await stats.dashboard(session)
    assert data["total_devices"] == 0
    assert data["top_week"] == []
    assert len(data["days"]) == 14


async def test_stats_page_requires_admin(client):
    resp = await client.get("/admin/stats", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "login" in resp.headers["location"]
