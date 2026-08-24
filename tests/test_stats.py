from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select

from app import stats
from app.config import settings
from app.db import SessionLocal
from app.models import ApiEvent, DailyStat, Device, Place, TripIntent


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
        await session.execute(delete(TripIntent))
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


# MARK: - Обещания политики конфиденциальности
#
# Тексту в api/legal.py верить нельзя, пока он ничем не подпёрт: до этих
# тестов таблица devices не чистилась вообще, а обещание «не дольше 30 дней»
# в политике уже стояло.


async def test_device_id_does_not_outlive_retention():
    """Номер устройства стирается вместе с его событиями.

    legal.py обещает, что дальше срока остаются «только общие числа по дням,
    без привязки к устройствам». Строка в devices — это и есть привязка.
    """
    await _clear()
    today = date.today()
    stale = today - timedelta(days=settings.stats_retention_days + 10)

    async with SessionLocal() as session:
        session.add_all(
            [
                Device(device="old-device", first_seen=stale),
                Device(device="live-device", first_seen=today - timedelta(days=1)),
                ApiEvent(kind="catalog", device="old-device", ts=_at(stale)),
                ApiEvent(kind="catalog", device="live-device", ts=_at(today)),
            ]
        )
        await session.commit()

        await stats.purge(session, today=today)

        left = (await session.execute(select(Device.device))).scalars().all()
    assert left == ["live-device"]


async def test_device_seen_recently_survives_old_first_seen():
    """Давний, но живой пользователь не теряется.

    Условие на first_seen одно ничего не решает: человек мог поставить
    приложение полгода назад и открыть его сегодня.
    """
    await _clear()
    today = date.today()
    long_ago = today - timedelta(days=200)

    async with SessionLocal() as session:
        session.add_all(
            [
                Device(device="loyal", first_seen=long_ago),
                ApiEvent(kind="catalog", device="loyal", ts=_at(today)),
            ]
        )
        await session.commit()

        await stats.purge(session, today=today)

        left = (await session.execute(select(Device.device))).scalars().all()
    assert left == ["loyal"]


async def test_past_intents_are_purged():
    """Прошедшие отметки «пойду» не живут вечно.

    Снять их человек не может: приложение отдаёт только будущие даты,
    а добавить прошедшую запрещает. Значит удалять должен сервер.
    """
    await _clear()
    today = date.today()
    async with SessionLocal() as session:
        place = (await session.execute(select(Place).limit(1))).scalar_one()
        session.add_all(
            [
                TripIntent(
                    place_id=place.id,
                    day=today - timedelta(days=settings.stats_retention_days + 5),
                    device_id="d-old",
                ),
                TripIntent(
                    place_id=place.id, day=today + timedelta(days=3), device_id="d-new"
                ),
            ]
        )
        await session.commit()

        await stats.purge(session, today=today)

        left = (await session.execute(select(TripIntent.device_id))).scalars().all()
    assert left == ["d-new"]


async def test_devices_ever_survives_purge():
    """«Всего устройств» не проседает, когда строки устройств вычищены.

    Число складывается из дневных new_devices — они обезличены и живут
    вечно, поэтому история не теряется вместе с идентификаторами.
    """
    await _clear()
    today = date.today()
    async with SessionLocal() as session:
        session.add_all(
            [
                DailyStat(day=today - timedelta(days=40), new_devices=7),
                DailyStat(day=today - timedelta(days=2), new_devices=3),
                # Появилось после последнего досчитанного дня — ещё не в агрегатах
                Device(device="fresh", first_seen=today),
            ]
        )
        await session.commit()

        assert await stats._devices_ever(session) == 11


async def test_rotation_prunes_even_when_aggregate_already_exists():
    """Чистка не срывается из-за гонки двух воркеров.

    Юнит поднимает uvicorn с двумя воркерами, и каждый крутит свою ротацию.
    Раньше проигравший ронял транзакцию на daily_stats_pkey и утаскивал
    за собой удаление сырья — ровно это и случилось на бою 19 августа.
    """
    await _clear()
    today = date.today()
    yesterday = today - timedelta(days=1)
    stale = datetime.now().astimezone() - timedelta(
        days=settings.stats_retention_days + 5
    )

    async with SessionLocal() as session:
        # Агрегат за вчера уже записан — как если бы соседний воркер успел первым
        session.add(DailyStat(day=yesterday, active_devices=1))
        session.add_all(
            [
                ApiEvent(kind="catalog", device="d", ts=_at(yesterday)),
                ApiEvent(kind="catalog", device="d", ts=stale),
            ]
        )
        await session.commit()

        await stats.rotate(session, today=today)

        left = (
            await session.execute(select(func.count()).select_from(ApiEvent))
        ).scalar_one()
    assert left == 1


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
