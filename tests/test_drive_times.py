"""Матрица «город × место»: скрипт без сети и эндпоинт.

Роутер подменяется функцией: тесты не ходят в OSRM. Проверяется контракт
скрипта — без --apply ничего не пишется, пары без дороги не пишутся, шум
в пределах пяти минут не считается изменением, --tashkent обновляет поля
мест — и что эндпоинт отдаёт города на двух языках и матрицу только по
опубликованным местам. В конце модуль возвращает базу в прежнее состояние.
"""

import pytest
from sqlalchemy import delete, select

from app.cities import BY_CODE, CITIES, HUBS, TASHKENT, hub_of
from app.db import SessionLocal
from app.models import CityDriveTime, Place, PlaceDriveTime
from seed.enrich_drive_times import batches, run, run_hubs


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
        await session.execute(delete(CityDriveTime))
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
        "code": "tashkent", "name": "Ташкент", "from": "из Ташкента", "to": "до Ташкента",
        "lat": TASHKENT.lat, "lng": TASHKENT.lng, "area": "Ташкентская область",
    }
    assert uz["cities"][0]["from"] == "Toshkentdan" and uz["cities"][0]["area"] == "Toshkent viloyati"
    assert uz["cities"][0]["to"] == "Toshkentgacha"
    assert "test-waterfall" in ru["matrix"]
    cell = ru["matrix"]["test-waterfall"]["samarkand"]
    assert isinstance(cell[0], int) and isinstance(cell[1], float)
    resp = await client.get("/api/v1/drive-times")
    assert "max-age=86400" in resp.headers["cache-control"]


# --- Хабы: до какого центра ехать и сколько ---


def test_hubs_are_tashkent_and_regional_centres():
    assert HUBS[0] == "tashkent"
    # Ташкент плюс двенадцать центров; спутники столицы хабами не бывают
    assert len(HUBS) == 13
    assert "chirchiq" not in HUBS and "samarkand" in HUBS
    assert all(BY_CODE[code].area_ru == "Другие области" for code in HUBS[1:])


def test_hub_of_sends_tashkent_region_to_the_capital():
    # Житель области уже в своём хабе — пересадка ему не предлагается
    assert hub_of("chirchiq") == "tashkent"
    assert hub_of("angren") == "tashkent"
    assert hub_of("tashkent") == "tashkent"
    # Областной центр сам себе хаб
    assert hub_of("samarkand") == "samarkand"
    assert hub_of("termez") == "termez"
    # Неизвестный код не роняет расчёт
    assert hub_of("atlantis") == "tashkent"


async def test_hub_matrix_skips_self_pairs_and_writes_the_rest():
    stats = await run_hubs(apply=True, fn=fake(), pause=0)
    async with SessionLocal() as session:
        rows = (await session.execute(select(CityDriveTime))).scalars().all()
    assert stats["added"] == len(rows)
    # Ни одной пары «город сам в себя»: ехать некуда
    assert all(r.origin != r.hub for r in rows)
    # Каждый город доезжает до каждого чужого хаба, кроме пар без дороги
    assert len(rows) == len(CITIES) * len(HUBS) - len(HUBS) - stats["missing"]


async def test_endpoint_exposes_hubs_and_city_matrix(client):
    await run_hubs(apply=True, fn=fake(), pause=0)
    body = (await client.get("/api/v1/drive-times")).json()
    assert body["hubs"] == list(HUBS)
    # Самаркандец видит, сколько ему до Ташкента, и не видит дороги в себя
    assert "tashkent" in body["city_matrix"]["samarkand"]
    assert "samarkand" not in body["city_matrix"].get("samarkand", {})
    cell = body["city_matrix"]["samarkand"]["tashkent"]
    assert isinstance(cell[0], int) and isinstance(cell[1], float)


def test_case_forms_are_derived_for_every_city():
    """«до Ташкента» выводится из «из Ташкента»: там уже родительный падеж.

    Правило механическое, поэтому проверяем весь справочник — несклоняемые
    «Навои» и «Карши» тоже обязаны пройти.
    """
    for c in CITIES:
        # «до из Ташкента» — ровно та ошибка, ради которой форма и заведена
        assert c.to_ru.startswith("до ") and not c.to_ru.startswith("до из")
        assert c.to_uz.endswith("gacha") and not c.to_uz.endswith("dangacha")
    assert BY_CODE["navoi"].to_ru == "до Навои"
    assert BY_CODE["karshi"].to_uz == "Qarshigacha"
