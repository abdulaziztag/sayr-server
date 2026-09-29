"""Обвязка вокруг ручек: документация API, защитные заголовки, мусор во входе."""

import pytest


@pytest.mark.parametrize("url", ["/docs", "/redoc", "/openapi.json"])
async def test_документация_api_на_бою_закрыта(client, url):
    """Схема раздавала всё API, включая ручки, выключенные флагами"""
    assert (await client.get(url)).status_code == 404


async def test_nosniff_всем_а_hsts_только_по_https(client):
    plain = await client.get("/healthz")
    assert plain.headers["x-content-type-options"] == "nosniff"
    # По http браузер HSTS всё равно не примет, а локально он только мешал бы
    assert "strict-transport-security" not in plain.headers

    # Так приходит запрос через nginx: снаружи https, до приложения — http
    proxied = await client.get("/healthz", headers={"X-Forwarded-Proto": "https"})
    assert proxied.headers["strict-transport-security"].startswith("max-age=")


@pytest.mark.parametrize("url", ["/admin/login", "/seasons/review"])
async def test_админку_и_проверку_нельзя_вставить_в_чужую_рамку(client, url):
    resp = await client.get(url)
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


async def test_остальной_сайт_рамками_не_ограничен(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "x-frame-options" not in resp.headers


async def test_nul_в_адресе_это_ошибка_запроса_а_не_сервера(client):
    """Строку с NUL-байтом база не принимает, и раньше это была пятисотка.
    Страховка общая, на всё приложение: проверяем её на ручке, где своей
    проверки входа нет и не будет"""
    resp = await client.get("/r/a%00b")
    assert resp.status_code == 422
    assert resp.json() == {"detail": "bad_input"}


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/places/a%00b",
        "/api/v1/places?q=a%00b",
        "/p/a%00b",
        # Число, которое не лезет в bigint: OFFSET его не примет
        "/api/v1/places?offset=99999999999999999999",
    ],
)
async def test_мусор_во_входе_не_роняет_ручки(client, url):
    # Не 422 строго: у части ручек появляется своя проверка, и она вправе
    # ответить по-своему. Важно, что это не ошибка сервера
    assert (await client.get(url)).status_code < 500
