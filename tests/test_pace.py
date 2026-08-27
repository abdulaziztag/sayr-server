"""Как прошёл выход: подтверждение и темп.

Главное, что здесь проверяется, — что счётчик по месту не двоится.
Человек правит свой ответ тапом по записи в истории, значит поправка
на один и тот же поход приходит не один раз, а счётчик обязан остаться
счётчиком людей, а не нажатий.
"""

from datetime import date, timedelta

from sqlalchemy import delete, select

from app import stats
from app.db import SessionLocal
from app.models import ApiEvent, DailyStat, Device, PlacePaceStats, Place, TripIntent

SLUG = "test-peak"
DEVICE = "dev-pace-0001"


async def _clear():
    async with SessionLocal() as session:
        await session.execute(delete(TripIntent))
        await session.execute(delete(PlacePaceStats))
        await session.execute(delete(ApiEvent))
        await session.execute(delete(DailyStat))
        await session.execute(delete(Device))
        await session.commit()


async def _counters(slug: str = SLUG) -> tuple[int, int, int] | None:
    async with SessionLocal() as session:
        place_id = (
            await session.execute(select(Place.id).where(Place.slug == slug))
        ).scalar_one()
        row = (
            await session.execute(
                select(PlacePaceStats).where(PlacePaceStats.place_id == place_id)
            )
        ).scalar_one_or_none()
        return None if row is None else (row.faster, row.expected, row.slower)


async def _intent(client, day: date, device: str = DEVICE, slug: str = SLUG):
    """Отметка «Пойду» — без неё поправку слать некому."""
    resp = await client.post(
        f"/api/v1/places/{slug}/intents", json={"date": day.isoformat(), "device_id": device}
    )
    assert resp.status_code == 200


async def _pace(client, day: date, went: bool, pace: str | None, device: str = DEVICE):
    return await client.post(
        f"/api/v1/places/{SLUG}/pace",
        json={
            "date": day.isoformat(),
            "device_id": device,
            "went": went,
            "pace": pace,
        },
    )


async def test_pace_needs_an_intent(client):
    """Поправку присылает тот, кто планировал, — иначе накрутить можно с нуля."""
    await _clear()
    resp = await _pace(client, date.today(), went=True, pace="slower")
    assert resp.status_code == 404
    assert await _counters() is None


async def test_first_answer_counts_once(client):
    await _clear()
    today = date.today()
    await _intent(client, today)
    assert (await _pace(client, today, went=True, pace="slower")).status_code == 200
    assert await _counters() == (0, 0, 1)


async def test_same_answer_twice_does_not_double(client):
    await _clear()
    today = date.today()
    await _intent(client, today)
    await _pace(client, today, went=True, pace="slower")
    await _pace(client, today, went=True, pace="slower")
    assert await _counters() == (0, 0, 1)


async def test_changed_answer_moves_the_vote(client):
    """Правка ответа переносит голос, а не добавляет второй."""
    await _clear()
    today = date.today()
    await _intent(client, today)
    await _pace(client, today, went=True, pace="slower")
    await _pace(client, today, went=True, pace="faster")
    assert await _counters() == (1, 0, 0)


async def test_did_not_go_clears_the_vote(client):
    """«Не пошёл» снимает прежний голос: темпа у несостоявшегося выхода нет."""
    await _clear()
    today = date.today()
    await _intent(client, today)
    await _pace(client, today, went=True, pace="expected")
    assert await _counters() == (0, 1, 0)

    await _pace(client, today, went=False, pace="expected")
    assert await _counters() == (0, 0, 0)

    async with SessionLocal() as session:
        intent = (await session.execute(select(TripIntent))).scalar_one()
        assert intent.went is False
        assert intent.pace is None


async def test_two_devices_count_separately(client):
    await _clear()
    today = date.today()
    await _intent(client, today, device=DEVICE)
    await _intent(client, today, device="dev-pace-0002")
    await _pace(client, today, went=True, pace="slower", device=DEVICE)
    await _pace(client, today, went=True, pace="slower", device="dev-pace-0002")
    assert await _counters() == (0, 0, 2)


async def test_counter_survives_purge(client):
    """Личная строка живёт свои 30 дней, накопленная картина — навсегда."""
    await _clear()
    long_ago = date.today() - timedelta(days=400)

    async with SessionLocal() as session:
        place_id = (
            await session.execute(select(Place.id).where(Place.slug == SLUG))
        ).scalar_one()
        session.add(TripIntent(place_id=place_id, day=long_ago, device_id=DEVICE))
        await session.commit()

    await _pace(client, long_ago, went=True, pace="faster")
    assert await _counters() == (1, 0, 0)

    async with SessionLocal() as session:
        await stats.purge(session)
        await session.commit()

    async with SessionLocal() as session:
        assert (await session.execute(select(TripIntent))).scalars().all() == []
    assert await _counters() == (1, 0, 0)


async def test_old_clients_see_no_change(client):
    """Отметка и её снятие отвечают ровно тем же, чем отвечали."""
    await _clear()
    today = date.today()
    resp = await client.post(
        f"/api/v1/places/{SLUG}/intents",
        json={"date": today.isoformat(), "device_id": DEVICE},
    )
    assert resp.status_code == 200
    assert set(resp.json()) == {"days"}
    assert resp.json()["days"] == [{"date": today.isoformat(), "count": 1, "mine": True}]


async def test_answer_horizon_matches_get(client):
    """Ответ после отметки не короче обычного: раньше здесь было 60 против 62."""
    await _clear()
    far = date.today() + timedelta(days=61)
    await _intent(client, far)
    listed = await client.get(
        f"/api/v1/places/{SLUG}/intents", params={"device_id": DEVICE}
    )
    posted = await client.post(
        f"/api/v1/places/{SLUG}/intents",
        json={"date": far.isoformat(), "device_id": DEVICE},
    )
    assert posted.json() == listed.json()
