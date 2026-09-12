"""Планы по дням: что отдаёт деталь места и как планы живут в базе.

Главное здесь — порядок и язык: клиент рисует станции подряд, как пришли,
и подставляет узбекский только там, где он есть.
"""

from datetime import time

from fastapi_storages import StorageFile
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.config import GPX_DIR
from app.db import SessionLocal
from app.models import (Difficulty, Place, PlaceCategory, PlacePlan, PlaceTrack, PlanDay,
                        PlanStep, PlanStepKind, gpx_storage)

LAKE = "test-lake"

_GPX = b"""<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">
<trk><name>t</name><trkseg>
<trkpt lat="41.62" lon="70.10"><ele>1500</ele></trkpt>
<trkpt lat="41.63" lon="70.11"><ele>1600</ele></trkpt>
</trkseg></trk></gpx>"""


async def _place_id(slug: str) -> int:
    async with SessionLocal() as session:
        return (await session.execute(select(Place.id).where(Place.slug == slug))).scalar_one()


async def _track(slug: str, name: str) -> int:
    (GPX_DIR / "plan-test.gpx").write_bytes(_GPX)
    async with SessionLocal() as session:
        track = PlaceTrack(
            place_id=await _place_id(slug), name=name,
            gpx_file=StorageFile(name="plan-test.gpx", storage=gpx_storage),
        )
        session.add(track)
        await session.commit()
        return track.id


async def _plan(slug: str = LAKE, title: str = "Классика", track_id: int | None = None,
                sort_order: int = 0) -> int:
    """План на два дня со станциями всех семи видов."""
    async with SessionLocal() as session:
        plan = PlacePlan(place_id=await _place_id(slug), title=title, title_uz="Klassika",
                         sort_order=sort_order)
        day1 = PlanDay(n=1, title="Подход", title_uz="Yaqinlashish", track_id=track_id)
        day1.steps = [
            PlanStep(sort_order=10, kind=PlanStepKind.depart, at=time(2, 0),
                     title="Выехать из Ташкента", title_uz="Toshkentdan chiqish"),
            PlanStep(sort_order=20, kind=PlanStepKind.point, at=time(5, 30),
                     title="Погранпост", sub="проверка пропусков",
                     sub_uz="ruxsatnomalar tekshiruvi"),
            PlanStep(sort_order=30, kind=PlanStepKind.hike, minutes=240, title="Подход к лагерю"),
            PlanStep(sort_order=40, kind=PlanStepKind.night, at=time(14, 0),
                     title="Базовый лагерь · 3220 м"),
        ]
        day2 = PlanDay(n=2, title="Возвращение", reversed=True, track_id=track_id)
        day2.steps = [
            PlanStep(sort_order=10, kind=PlanStepKind.summit, at=time(9, 0), title="Вершина"),
            PlanStep(sort_order=20, kind=PlanStepKind.road, minutes=450, title="Дорога"),
            PlanStep(sort_order=30, kind=PlanStepKind.home, title="Дома"),
        ]
        # День 2 добавлен первым: порядок в ответе обязан идти по n, а не по id
        plan.days = [day2, day1]
        session.add(plan)
        await session.commit()
        return plan.id


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(PlacePlan))
        await session.execute(delete(PlaceTrack).where(PlaceTrack.name.like("plan-test%")))
        await session.execute(delete(Place).where(Place.slug == "test-plan-temp"))
        await session.commit()


async def test_место_без_планов_отдаёт_пустой_список(client):
    detail = (await client.get(f"/api/v1/places/{LAKE}")).json()
    assert detail["plans"] == []


async def test_план_в_детали_по_порядку_и_на_двух_языках(client):
    try:
        await _plan(title="Второй", sort_order=5)
        await _plan(title="Первый", sort_order=1)
        detail = (await client.get(f"/api/v1/places/{LAKE}")).json()
        assert [p["title"] for p in detail["plans"]] == ["Первый", "Второй"], "по sort_order"
        plan = detail["plans"][0]
        assert plan["is_draft"] is False
        assert [d["n"] for d in plan["days"]] == [1, 2], "дни по номеру, а не по id"
        day1 = plan["days"][0]
        assert day1["track_id"] is None and day1["reversed"] is False
        assert [s["kind"] for s in day1["steps"]] == ["depart", "point", "hike", "night"]
        depart, post, hike, night = day1["steps"]
        assert depart["time"] == "2:00" and depart["minutes"] is None, "без ведущего нуля"
        assert post["sub"] == "проверка пропусков"
        assert hike["time"] is None and hike["minutes"] == 240
        assert night["time"] == "14:00"
        assert plan["days"][1]["reversed"] is True
        assert plan["days"][1]["steps"][2] == {
            "kind": "home", "time": None, "minutes": None, "title": "Дома", "sub": None}

        uz = (await client.get(f"/api/v1/places/{LAKE}?lang=uz")).json()["plans"][0]
        assert uz["title"] == "Klassika"
        assert uz["days"][0]["title"] == "Yaqinlashish"
        steps = uz["days"][0]["steps"]
        assert steps[0]["title"] == "Toshkentdan chiqish"
        assert steps[1]["sub"] == "ruxsatnomalar tekshiruvi"
        assert steps[2]["title"] == "Подход к лагерю", "нет перевода — русское"
        assert uz["days"][1]["title"] == "Возвращение"
    finally:
        await _clean()


async def test_удаление_трека_обнуляет_день_а_удаление_места_уносит_план(client):
    try:
        track_id = await _track(LAKE, "plan-test track")
        plan_id = await _plan(track_id=track_id)
        detail = (await client.get(f"/api/v1/places/{LAKE}")).json()
        assert detail["plans"][0]["days"][0]["track_id"] == track_id

        async with SessionLocal() as session:
            track = (await session.execute(
                select(PlaceTrack).where(PlaceTrack.id == track_id))).scalar_one()
            await session.delete(track)
            await session.commit()
        detail = (await client.get(f"/api/v1/places/{LAKE}")).json()
        assert detail["plans"][0]["days"][0]["track_id"] is None, "день остался, трек — нет"

        # Временное место: удалять фикстурное нельзя, оно нужно другим тестам
        async with SessionLocal() as session:
            lake = (await session.execute(select(Place).where(Place.slug == LAKE))).scalar_one()
            temp = Place(
                slug="test-plan-temp", name="Временное", category=PlaceCategory.lake,
                difficulty=Difficulty.easy, lat=lake.lat, lng=lake.lng,
                region_id=lake.region_id, short_desc="", is_published=True,
            )
            session.add(temp)
            await session.commit()
        temp_plan = await _plan(slug="test-plan-temp")
        async with SessionLocal() as session:
            await session.execute(delete(Place).where(Place.slug == "test-plan-temp"))
            await session.commit()
            left = (await session.execute(
                select(PlacePlan.id).where(PlacePlan.id.in_([temp_plan])))).all()
            assert left == [], "план ушёл вместе с местом"
            days = (await session.execute(select(PlanDay.id).where(PlanDay.plan_id == temp_plan))).all()
            assert days == []
            # А план озера жив
            assert (await session.execute(
                select(PlacePlan.id).where(PlacePlan.id == plan_id))).scalar_one() == plan_id
    finally:
        await _clean()


async def _first_day_id(plan_id: int) -> int:
    async with SessionLocal() as session:
        return (await session.execute(
            select(PlanDay.id).where(PlanDay.plan_id == plan_id).order_by(PlanDay.n))).scalars().first()


async def test_станция_создаётся_формой_админки_со_временем_и_без(client, admin_client):
    """Запасной путь: раздельные поля времени, длительности и языков."""
    try:
        plan_id = await _plan()
        day_id = await _first_day_id(plan_id)
        base = {"day": str(day_id), "sort_order": "50", "title_uz": "", "sub_uz": "", "save": "Save"}
        with_time = await admin_client.post(
            "/admin/plan-step/create",
            data={**base, "kind": "point", "at": "05:30", "minutes": "", "title": "Погранпост",
                  "sub": "проверка пропусков"},
            follow_redirects=False)
        assert with_time.status_code == 302, with_time.text[:600]
        without = await admin_client.post(
            "/admin/plan-step/create",
            data={**base, "sort_order": "60", "kind": "hike", "at": "", "minutes": "240",
                  "title": "Подход к лагерю", "sub": ""},
            follow_redirects=False)
        assert without.status_code == 302, without.text[:600]

        async with SessionLocal() as session:
            rows = (await session.execute(
                select(PlanStep).where(PlanStep.day_id == day_id, PlanStep.sort_order >= 50)
                .order_by(PlanStep.sort_order))).scalars().all()
        assert [(r.kind, r.at, r.minutes, r.sub) for r in rows] == [
            (PlanStepKind.point, time(5, 30), None, "проверка пропусков"),
            (PlanStepKind.hike, None, 240, None),
        ], "пустые поля формы — это NULL, а не пустые строки"
    finally:
        await _clean()


async def test_загрузчик_идемпотентен_и_привязывает_трек_по_имени(client):
    from seed.load_plans import load

    try:
        await _track(LAKE, "plan-test loader")
        data = {
            LAKE: {"plans": [{
                "title": "Из файла", "title_uz": "Fayldan", "is_draft": True,
                "days": [
                    {"n": 1, "title": "Подход", "track": "plan-test loader",
                     "steps": [{"kind": "depart", "time": "2:00", "title": "Выехать"},
                               {"kind": "hike", "minutes": 240, "title": "Пешком"}]},
                    {"n": 2, "title": "Обратно", "track": "такого трека нет", "reversed": True,
                     "steps": [{"kind": "home", "time": "19:00", "title": "Дома"}]},
                ]}]},
            "no-such-place": {"plans": [{"title": "Мимо", "days": []}]},
        }
        async with SessionLocal() as session:
            log = await load(data, session)
        assert any("no-such-place" in line and "!" in line for line in log), log
        assert any("такого трека нет" in line for line in log), log
        async with SessionLocal() as session:
            log2 = await load(data, session)
        assert not any("Мимо" in line for line in log2 if not line.startswith("!"))

        detail = (await client.get(f"/api/v1/places/{LAKE}")).json()
        assert len(detail["plans"]) == 1, "второй запуск не плодит план"
        plan = detail["plans"][0]
        assert plan["is_draft"] is True and plan["title"] == "Из файла"
        day1, day2 = plan["days"]
        assert day1["track_id"] is not None, "трек нашёлся по имени"
        assert day2["track_id"] is None and day2["reversed"] is True
        assert [s["kind"] for s in day1["steps"]] == ["depart", "hike"]
        assert day1["steps"][0]["time"] == "2:00"
        uz = (await client.get(f"/api/v1/places/{LAKE}?lang=uz")).json()["plans"][0]
        assert uz["title"] == "Fayldan"
    finally:
        await _clean()
