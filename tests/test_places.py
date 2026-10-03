async def test_list_all(client):
    resp = await client.get("/api/v1/places")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 5
    assert data[0]["name"] <= data[1]["name"]  # сортировка по имени без near


async def test_filter_category(client):
    resp = await client.get("/api/v1/places", params={"category": "waterfall"})
    slugs = [p["slug"] for p in resp.json()]
    assert slugs == ["test-waterfall"]


async def test_filter_multi_category(client):
    resp = await client.get(
        "/api/v1/places", params=[("category", "waterfall"), ("category", "peak")]
    )
    assert {p["slug"] for p in resp.json()} == {
        "test-waterfall",
        "test-peak",
        "test-alpine-peak",
    }


async def test_filter_difficulty_and_season(client):
    resp = await client.get("/api/v1/places", params={"difficulty": "easy", "season": "summer"})
    assert {p["slug"] for p in resp.json()} == {"test-waterfall", "test-lake"}


async def test_filter_kid_friendly(client):
    resp = await client.get("/api/v1/places", params={"kid_friendly": "true"})
    assert all(p["kid_friendly"] for p in resp.json())
    assert len(resp.json()) == 2


async def test_search_q(client):
    resp = await client.get("/api/v1/places", params={"q": "озеро"})
    assert [p["slug"] for p in resp.json()] == ["test-lake"]


async def test_near_radius_and_order(client):
    # 100 км от Ташкента: озеро (~60 км) и водопад/пик (~80–90 км), но не плато (~200 км)
    resp = await client.get(
        "/api/v1/places", params={"near": "41.31,69.28", "radius_km": 100}
    )
    slugs = [p["slug"] for p in resp.json()]
    assert "test-far-plateau" not in slugs
    assert slugs[0] == "test-lake"  # ближайшее — первым


async def test_near_validation(client):
    resp = await client.get("/api/v1/places", params={"near": "oops"})
    assert resp.status_code == 422


async def test_detail_fields(client):
    resp = await client.get("/api/v1/places/test-peak")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Тестовый пик"
    assert body["region_name"] == "Тестовый регион"
    assert body["difficulty"] == "hard"
    assert body["photos"] == []
    assert body["gpx_url"] is None
    assert body["has_gpx"] is False


async def test_detail_404(client):
    resp = await client.get("/api/v1/places/no-such-place")
    assert resp.status_code == 404


async def test_regions_with_counts(client):
    resp = await client.get("/api/v1/regions")
    assert resp.status_code == 200
    regions = resp.json()
    assert regions[0]["name"] == "Тестовый регион"
    assert regions[0]["places_count"] == 4
    assert regions[1]["name"] == "Дальний регион"
    assert regions[1]["places_count"] == 1


async def test_collections_field_is_present_and_empty_by_default(client):
    """Поле едет всегда: старые кэши клиентов декодируют его со значением по умолчанию."""
    items = (await client.get("/api/v1/places")).json()
    assert items and all(item["collections"] == [] for item in items)
    detail = (await client.get(f"/api/v1/places/{items[0]['slug']}")).json()
    assert detail["collections"] == []


# --- Кривой ввод: 404 и 422, а не 500 ------------------------------------------
#
# До проверок всё это доходило до базы и падало там DataError: NUL Postgres
# в тексте не принимает, числа за пределами int4 и бесконечность в cos() —
# тоже. Человек получал 500, а журнал — трассировку на каждый такой запрос.


async def test_слаг_не_по_форме_это_не_найдено(client):
    for path in (
        "/api/v1/places/a%00b",
        "/api/v1/places/a%00b/weather",
        "/api/v1/places/a%00b/intents",
        "/p/a%00b",
        "/api/v1/places/Test-Peak",
    ):
        resp = await client.get(path)
        assert resp.status_code == 404, path
    resp = await client.post(
        "/api/v1/places/a%00b/intents", json={"date": "2030-01-01", "device_id": "device-0001"}
    )
    assert resp.status_code == 404


async def test_админка_не_сохраняет_слаг_не_по_форме():
    """Раз ручки отсекают слаг не по форме ещё до базы, завести такой
    нельзя: место стояло бы в каталоге, но не открывалось бы ни
    в приложении, ни по ссылке /p/."""
    import pytest

    from app.admin import PlaceAdmin
    from app.models import Place

    view = PlaceAdmin()
    for bad in ("Chimgan", "big_lake", "-lake", "чимган", "a" * 121, ""):
        with pytest.raises(ValueError):
            await view.on_model_change({"slug": bad}, Place(), True, None)
    data = {"slug": " chimgan-2 "}
    await view.on_model_change(data, Place(), True, None)
    assert data["slug"] == "chimgan-2"


async def test_nul_в_тексте_запроса_это_422(client):
    resp = await client.get("/api/v1/places", params={"q": "во\x00да"})
    assert resp.status_code == 422
    resp = await client.get(
        "/api/v1/places/test-peak/intents", params={"device_id": "dev\x00ice-01"}
    )
    assert resp.status_code == 422
    resp = await client.post(
        "/api/v1/places/test-peak/intents",
        json={"date": "2030-01-01", "device_id": "dev\x00ice-01"},
    )
    assert resp.status_code == 422
    resp = await client.delete(
        "/api/v1/places/test-peak/intents",
        params={"date": "2030-01-01", "device_id": "dev\x00ice-01"},
    )
    assert resp.status_code == 422


async def test_числа_за_пределами_базы_это_422(client):
    for params in ({"offset": 10**20}, {"region_id": 10**20}, {"region_id": -(10**20)}):
        resp = await client.get("/api/v1/places", params=params)
        assert resp.status_code == 422, params


async def test_near_только_земные_координаты(client):
    for near in ("inf,0", "0,-inf", "nan,0", "91,0", "0,181"):
        resp = await client.get("/api/v1/places", params={"near": near})
        assert resp.status_code == 422, near
    resp = await client.get("/api/v1/places", params={"near": "-90,180"})
    assert resp.status_code == 200
    # Точка напротив водопада на шаре: косинус округляется чуть ниже −1,
    # и acos без зажима снизу ронял запрос
    resp = await client.get(
        "/api/v1/places", params={"near": "-41.62,-109.9", "radius_km": 1000}
    )
    assert resp.status_code == 200
    assert resp.json() == []


# Предел каталога. Установленные сборки обеих платформ просят limit=200
# и обновиться не могут, а мест уже под полторы сотни, и импорты добавляют
# их пачками: за двумя сотнями алфавитный хвост тихо пропал бы из списка,
# карты и офлайн-кэша. Поэтому их 200 сервер понимает как «весь каталог»,
# а новые сборки просят 1000 — столько он и отдаёт за раз


async def _add_tail_places(count: int):
    """Места в конце алфавита — именно их срезала бы обрезка"""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Difficulty, Place, PlaceCategory

    async with SessionLocal() as session:
        lake = (await session.execute(select(Place).where(Place.slug == "test-lake"))).scalar_one()
        session.add_all([
            Place(
                slug=f"test-tail-{i:03d}", name=f"Я хвост {i:03d}",
                category=PlaceCategory.lake, difficulty=Difficulty.easy,
                lat=lake.lat, lng=lake.lng, region_id=lake.region_id,
                short_desc="", is_published=True,
            )
            for i in range(count)
        ])
        await session.commit()


async def _drop_tail_places():
    from sqlalchemy import delete

    from app.db import SessionLocal
    from app.models import Place

    async with SessionLocal() as session:
        await session.execute(delete(Place).where(Place.slug.like("test-tail-%")))
        await session.commit()


async def test_limit_200_старых_сборок_отдаёт_весь_каталог(client):
    await _add_tail_places(200)
    try:
        resp = await client.get("/api/v1/places", params={"limit": 200})
        assert resp.status_code == 200
        slugs = [p["slug"] for p in resp.json()]
        assert len(slugs) == 205, "фикстуры и все двести добавленных — хвост не срезан"
        assert slugs[-1] == "test-tail-199"
        # Новые сборки просят тысячу — и получают то же
        resp = await client.get("/api/v1/places", params={"limit": 1000})
        assert resp.status_code == 200
        assert [p["slug"] for p in resp.json()] == slugs
    finally:
        await _drop_tail_places()


async def test_limit_до_тысячи_а_меньший_предел_режет_как_раньше(client):
    resp = await client.get("/api/v1/places", params={"limit": 1001})
    assert resp.status_code == 422
    resp = await client.get("/api/v1/places", params={"limit": 0})
    assert resp.status_code == 422
    everything = [p["slug"] for p in (await client.get("/api/v1/places")).json()]
    resp = await client.get("/api/v1/places", params={"limit": 3})
    assert [p["slug"] for p in resp.json()] == everything[:3]
    resp = await client.get("/api/v1/places", params={"limit": 3, "offset": 3})
    assert [p["slug"] for p in resp.json()] == everything[3:6]

