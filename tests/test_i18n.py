"""Двуязычный каталог: ?lang, фолбэк на русский и неизменность старого ответа.

В фикстурах переведены водопад и озеро, пик и плато — нет; у водопада
нет перевода только у «как добраться». На этом и проверяем, что фолбэк
работает по каждому полю отдельно.
"""


async def test_without_lang_nothing_changes(client):
    """Главное правило: старая сборка не должна заметить, что что-то произошло."""
    plain = await client.get("/api/v1/places")
    explicit_ru = await client.get("/api/v1/places", params={"lang": "ru"})
    assert plain.json() == explicit_ru.json()
    assert all(p["name"].startswith("Тест") or p["name"] == "Дальнее плато" for p in plain.json())


async def test_uz_returns_translation(client):
    resp = await client.get("/api/v1/places", params={"lang": "uz"})
    by_slug = {p["slug"]: p for p in resp.json()}
    assert by_slug["test-waterfall"]["name"] == "Test sharsharasi"
    assert by_slug["test-waterfall"]["short_desc"] == "Test uchun chiroyli sharshara"
    assert by_slug["test-waterfall"]["region_name"] == "Test viloyati"


async def test_uz_falls_back_to_russian(client):
    """Место без перевода показывается по-русски, а не пустым."""
    resp = await client.get("/api/v1/places", params={"lang": "uz"})
    by_slug = {p["slug"]: p for p in resp.json()}
    assert by_slug["test-peak"]["name"] == "Тестовый пик"
    assert by_slug["test-peak"]["short_desc"] == "Суровый пик для теста"


async def test_detail_falls_back_field_by_field(client):
    """У водопада переведено описание, но не «как добраться»."""
    resp = await client.get("/api/v1/places/test-waterfall", params={"lang": "uz"})
    data = resp.json()
    assert data["description_md"] == "Sharsharaning oʻzbekcha tavsifi"
    assert data["how_to_get_md"] == "Ехать на маршрутке"


async def test_regions_translated_and_fallback(client):
    resp = await client.get("/api/v1/regions", params={"lang": "uz"})
    assert [r["name"] for r in resp.json()] == ["Test viloyati", "Uzoq viloyat"]
    ru = await client.get("/api/v1/regions")
    assert [r["name"] for r in ru.json()] == ["Тестовый регион", "Дальний регион"]


async def test_order_stays_russian_under_uz(client):
    """Сортировка не зависит от языка: COLLATE C развалил бы смешанный список."""
    ru = [p["slug"] for p in (await client.get("/api/v1/places")).json()]
    uz = [p["slug"] for p in (await client.get("/api/v1/places", params={"lang": "uz"})).json()]
    assert ru == uz


async def test_search_finds_both_languages(client):
    """Узбекский интерфейс не мешает искать по русскому названию и наоборот."""
    by_ru = await client.get("/api/v1/places", params={"q": "озеро", "lang": "uz"})
    assert [p["slug"] for p in by_ru.json()] == ["test-lake"]
    by_uz = await client.get("/api/v1/places", params={"q": "koʻli", "lang": "ru"})
    assert [p["slug"] for p in by_uz.json()] == ["test-lake"]


async def test_unknown_lang_is_rejected(client):
    resp = await client.get("/api/v1/places", params={"lang": "en"})
    assert resp.status_code == 422


async def test_share_page_switches_language(client):
    ru = await client.get("/p/test-waterfall")
    assert '<html lang="ru">' in ru.text
    assert "Тестовый водопад" in ru.text
    assert "Открыть в приложении" in ru.text

    uz = await client.get("/p/test-waterfall", params={"lang": "uz"})
    assert '<html lang="uz">' in uz.text
    assert "Test sharsharasi" in uz.text
    assert "Ilovada ochish" in uz.text
    assert "sharshara" in uz.text  # категория тоже переведена
