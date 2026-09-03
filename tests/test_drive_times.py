"""Матрица «город × место»: скрипт без сети и эндпоинт.

Роутер подменяется функцией: тесты не ходят в OSRM. Проверяется контракт
скрипта — без --apply ничего не пишется, пары без дороги не пишутся, шум
в пределах пяти минут не считается изменением, --tashkent обновляет поля
мест — и что эндпоинт отдаёт города на двух языках и матрицу только по
опубликованным местам. В конце модуль возвращает базу в прежнее состояние.
"""

import pytest
from sqlalchemy import delete, select

from app.cities import CITIES, TASHKENT
from app.db import SessionLocal
from app.models import Place, PlaceDriveTime
from seed.enrich_drive_times import batches, run


def fake(offset: int = 0):
    """Минуты растут по индексам, у последнего города до первого места дороги нет."""

    def fn(origins, destinations):
        return [
            [
                None if (o == origins[-1] and d == destinations[0] and len(origins) > 1) else (60 + 10 * j + i + offset, 10.0 * (j + 1))
                for j, d in enumerate(destinations)
            ]
            for i, o in enumerate(origins)
        ]

    return fn


@pytest.fixture(scope="module", autouse=True)
async def restore_places():
    async with SessionLocal() as session:
        before = {p.id: (p.drive_minutes, p.drive_km) for p in (await session.execute(select(Place))).scalars()}
    yield
    async with SessionLocal() as session:
        await session.execute(delete(PlaceDriveTime))
        for p in (await session.execute(select(Place))).scalars():
            p.drive_minutes, p.drive_km = before[p.id]
        await session.commit()


async def _rows() -> int:
    async with SessionLocal() as session:
        return len((await session.execute(select(PlaceDriveTime))).scalars().all())


def test_batches_fit_the_limit_and_cover_every_pair():
    parts = batches(28, 126)
    assert len(parts) == 4
    assert all(len(s) + len(d) <= 100 for s, d in parts)
    covered = {(i, j) for s, d in parts for i in s for j in d}
    assert covered == {(i, j) for i in range(28) for j in range(126)}
    assert batches(1, 3) == [(range(0, 1), range(0, 3))]


async def test_dry_run_writes_nothing(capsys):
    stats = await run(apply=False, fn=fake(), pause=0)
    assert stats["added"] > 0 and stats["missing"] >= 1
    assert await _rows() == 0
    assert "к записи" in capsys.readouterr().out


async def test_apply_writes_pairs_and_skips_missing():
    async with SessionLocal() as session:
        n_places = len((await session.execute(select(Place))).scalars().all())
    stats = await run(apply=True, fn=fake(), pause=0)
    # одна пара без дороги в каждой пачке городов — их две
    assert stats["pairs"] == n_places * len(CITIES) - stats["missing"]
    assert await _rows() == stats["pairs"]


async def test_small_noise_is_not_a_change_but_big_is(capsys):
    stats = await run(apply=False, fn=fake(offset=3), pause=0)
    assert stats["changed"] == 0
    stats = await run(apply=False, fn=fake(offset=6), pause=0)
    assert stats["changed"] == stats["pairs"]
    assert "→" in capsys.readouterr().out


async def test_tashkent_flag_updates_place_fields():
    await run(apply=True, tashkent=True, fn=fake(offset=100), pause=0)
    async with SessionLocal() as session:
        rows = {
            (r.place_id, r.city): r
            for r in (await session.execute(select(PlaceDriveTime))).scalars()
        }
        for p in (await session.execute(select(Place))).scalars():
            row = rows.get((p.id, TASHKENT.code))
            assert row is not None
            assert p.drive_minutes == row.minutes and p.drive_km == row.km


async def test_endpoint_returns_cities_in_both_languages_and_published_matrix(client):
    ru = (await client.get("/api/v1/drive-times")).json()
    uz = (await client.get("/api/v1/drive-times?lang=uz")).json()
    assert len(ru["cities"]) == len(CITIES) == len(uz["cities"])
    assert ru["cities"][0] == {
        "code": "tashkent", "name": "Ташкент", "from": "из Ташкента",
        "lat": TASHKENT.lat, "lng": TASHKENT.lng, "area": "Ташкентская область",
    }
    assert uz["cities"][0]["from"] == "Toshkentdan" and uz["cities"][0]["area"] == "Toshkent viloyati"
    assert "test-waterfall" in ru["matrix"]
    cell = ru["matrix"]["test-waterfall"]["samarkand"]
    assert isinstance(cell[0], int) and isinstance(cell[1], float)
    resp = await client.get("/api/v1/drive-times")
    assert "max-age=86400" in resp.headers["cache-control"]
