from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select

from app import stats
from app.config import settings
from app.db import SessionLocal
from app.models import (
    ApiEvent,
    CohortRetention,
    DailyCount,
    DailyPlatform,
    DailyStat,
    Device,
    Place,
    TripIntent,
)


async def _events(**where):
    async with SessionLocal() as session:
        query = select(ApiEvent)
        if where.get("kind"):
            query = query.where(ApiEvent.kind == where["kind"])
        return (await session.execute(query)).scalars().all()


async def _clear():
    async with SessionLocal() as session:
        for table in (ApiEvent, DailyStat, DailyCount, DailyPlatform, CohortRetention, Device, TripIntent):
            await session.execute(delete(table))
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


def test_parse_app_header():
    info = stats.parse_app_header("android/1.7.0 ru 14")
    assert (info.platform, info.version, info.lang, info.os_major, info.debug) == (
        "android", "1.7.0", "ru", "14", False)
    debug = stats.parse_app_header("ios/1.7.1-debug uz 26.0")
    assert debug.debug and debug.version == "1.7.1" and debug.os_major == "26.0"
    bare = stats.parse_app_header("ios/1.7.1")
    assert bare.lang is None and bare.os_major is None
    for bad in ("", None, "windows/1.0", "ios/abc", "ios 1.7.1", "android/1.7.0 russian x"):
        parsed = stats.parse_app_header(bad)
        assert parsed is None or (parsed.lang is None and parsed.os_major is None), bad


def test_clean_mark():
    assert stats.clean_mark("gorets") == "gorets"
    assert stats.clean_mark("lider-hikers_2") == "lider-hikers_2"
    assert stats.clean_mark("<script>alert(1)</script>") == "scriptalert1script"
    assert stats.clean_mark("") is None and stats.clean_mark(None) is None
    assert len(stats.clean_mark("x" * 100)) == 32


async def test_header_on_regular_request_touches_device(client):
    await _clear()
    await client.get("/api/v1/places", headers={
        "X-Device-Id": "dev-app-header", "X-Sayr-App": "android/1.7.0 ru 14"})
    async with SessionLocal() as session:
        device = (await session.execute(select(Device))).scalar_one()
    assert (device.platform, device.app_version, device.lang, device.os_major) == (
        "android", "1.7.0", "ru", "14")
    assert device.last_seen == date.today()


async def test_debug_request_is_not_recorded(client):
    await _clear()
    await client.get("/api/v1/places", headers={
        "X-Device-Id": "dev-debug-get", "X-Sayr-App": "android/1.7.0-debug ru 14"})
    assert await _events(kind="catalog") == []


# MARK: - Универсальные свёртки и когорты


async def test_daily_counts_roll_up_by_kind_and_key():
    await _clear()
    today = date.today()
    yesterday = today - timedelta(days=1)
    async with SessionLocal() as session:
        session.add_all([
            ApiEvent(kind="nav_start", slug="a", device="d1", ts=_at(yesterday)),
            ApiEvent(kind="nav_start", slug="a", device="d1", ts=_at(yesterday)),
            ApiEvent(kind="nav_start", slug="a", device="d2", ts=_at(yesterday)),
            ApiEvent(kind="catalog", device="d1", ts=_at(yesterday)),
            ApiEvent(kind="nav_start", slug="a", device="d9", ts=_at(today)),  # сегодня не закрыт
        ])
        await session.commit()
        await stats.rotate(session, today=today)
        rows = {
            (r.kind, r.key): (r.events, r.devices)
            for r in (await session.execute(select(DailyCount))).scalars().all()
        }
    assert rows == {("nav_start", "a"): (3, 2), ("catalog", ""): (1, 1)}


async def test_rollup_day_overwrites_instead_of_adding():
    await _clear()
    yesterday = date.today() - timedelta(days=1)
    async with SessionLocal() as session:
        session.add(ApiEvent(kind="favorite", slug="z", device="d1", ts=_at(yesterday)))
        await session.commit()
        await stats.rollup_day(session, yesterday)
        await stats.rollup_day(session, yesterday)
        await session.commit()
        row = (await session.execute(select(DailyCount))).scalar_one()
    assert (row.events, row.devices) == (1, 1)


async def test_daily_platform_splits_known_and_unknown():
    await _clear()
    today = date.today()
    yesterday = today - timedelta(days=1)
    async with SessionLocal() as session:
        session.add_all([
            Device(device="ios-1", first_seen=yesterday, platform="ios"),
            Device(device="and-1", first_seen=yesterday - timedelta(days=5), platform="android"),
            Device(device="old-1", first_seen=yesterday),  # сборка без заголовка
            ApiEvent(kind="place", slug="a", device="ios-1", ts=_at(yesterday)),
            ApiEvent(kind="place", slug="a", device="and-1", ts=_at(yesterday)),
            ApiEvent(kind="place", slug="a", device="old-1", ts=_at(yesterday)),
            ApiEvent(kind="landing", device=None, ts=_at(yesterday)),  # без устройства — не в счёт
        ])
        await session.commit()
        await stats.rotate(session, today=today)
        rows = {
            r.platform: (r.active, r.new)
            for r in (await session.execute(select(DailyPlatform))).scalars().all()
        }
    assert rows == {"ios": (1, 1), "android": (1, 0), "unknown": (1, 1)}


async def test_cohorts_over_three_weeks():
    """A и B пришли в неделю 1, C — в неделю 2; вернулся на второй неделе только A."""
    await _clear()
    today = date.today()
    this_monday = stats.week_start(today)
    w2 = this_monday - timedelta(weeks=1)   # последняя закрытая неделя
    w1 = w2 - timedelta(weeks=1)
    async with SessionLocal() as session:
        session.add_all([
            Device(device="A", first_seen=w1),
            Device(device="B", first_seen=w1 + timedelta(days=2)),
            Device(device="C", first_seen=w2 + timedelta(days=1)),
            ApiEvent(kind="app_open", device="A", ts=_at(w1)),
            ApiEvent(kind="app_open", device="B", ts=_at(w1 + timedelta(days=2))),
            ApiEvent(kind="app_open", device="A", ts=_at(w2 + timedelta(days=3))),
            ApiEvent(kind="app_open", device="C", ts=_at(w2 + timedelta(days=1))),
        ])
        await session.commit()
        await stats.rotate_weeks(session, today=today)
        rows = {
            (r.cohort_week, r.week_index): r.devices
            for r in (await session.execute(select(CohortRetention))).scalars().all()
        }
    assert rows == {(w1, 0): 2, (w1, 1): 1, (w2, 0): 1}


async def test_rotate_weeks_skips_weeks_already_counted():
    await _clear()
    today = date.today()
    w2 = stats.week_start(today) - timedelta(weeks=1)
    async with SessionLocal() as session:
        session.add_all([
            Device(device="A", first_seen=w2),
            ApiEvent(kind="app_open", device="A", ts=_at(w2)),
        ])
        await session.commit()
        await stats.rotate_weeks(session, today=today)
        # Появилось новое устройство задним числом — закрытую неделю не пересчитываем
        session.add_all([
            Device(device="Z", first_seen=w2),
            ApiEvent(kind="app_open", device="Z", ts=_at(w2 + timedelta(days=1))),
        ])
        await session.commit()
        await stats.rotate_weeks(session, today=today)
        row = (await session.execute(select(CohortRetention))).scalar_one()
    assert (row.cohort_week, row.week_index, row.devices) == (w2, 0, 1)


async def test_backfill_fills_days_the_rotation_skips():
    """На бою у прошлых дней daily_stats уже есть, и ротация их не трогает."""
    from app import stats_backfill

    await _clear()
    today = date.today()
    d3 = today - timedelta(days=3)
    async with SessionLocal() as session:
        session.add_all([
            DailyStat(day=d3, active_devices=1, new_devices=0, place_opens=1),
            ApiEvent(kind="place", slug="a", device="d1", ts=_at(d3)),
            Device(device="d1", first_seen=d3 - timedelta(days=10)),
        ])
        await session.commit()
        await stats.rotate(session, today=today)
        assert (await session.execute(select(DailyCount))).scalars().all() == []

    done = await stats_backfill.run(days=5, today=today)
    assert d3 in done and len(done) == 5
    async with SessionLocal() as session:
        row = (await session.execute(select(DailyCount))).scalar_one()
    assert (row.day, row.kind, row.key, row.events, row.devices) == (d3, "place", "a", 1, 1)

