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
