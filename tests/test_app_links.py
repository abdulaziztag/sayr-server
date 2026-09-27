"""Ссылка на место с телефона без приложения: мимо страницы — в приложение
через intent:// или в магазин; компьютер и роботы превью видят страницу."""

from urllib.parse import unquote

import pytest

from app.config import settings

STORE = "https://apps.apple.com/app/id6800192455"
PLAY = "https://play.google.com/store/apps/details?id=uz.sayr.android"

IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
CHROME = (
    "Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36"
)
WEBVIEW = (
    "Mozilla/5.0 (Linux; Android 14; SM-A546E Build/UP1A; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/129.0.0.0 Mobile Safari/537.36"
)
GOOGLEBOT = (
    "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36 "
    "(compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)
TELEGRAM = "TelegramBot (like TwitterBot)"
IMESSAGE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) facebookexternalhit/1.1 Facebot Twitterbot/1.0"
DESKTOP = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)


@pytest.fixture
def stores(monkeypatch):
    monkeypatch.setattr(settings, "app_store_url", STORE)
    monkeypatch.setattr(settings, "play_store_url", PLAY)
    monkeypatch.setattr(settings, "public_url", "https://sayr.info")
    monkeypatch.setattr(settings, "ios_store_redirect", True)


async def _open(client, ua: str, slug: str = "test-waterfall"):
    return await client.get(f"/p/{slug}", headers={"user-agent": ua}, follow_redirects=False)


async def test_iphone_уходит_в_app_store_с_меткой(client, stores):
    r = await _open(client, IPHONE)
    assert r.status_code == 302
    assert r.headers["location"] == "https://sayr.info/go/ios?from=share"
    assert r.headers["vary"] == "User-Agent"


async def test_iphone_без_универсальных_ссылок_видит_страницу(client, stores, monkeypatch):
    # Старые версии для iOS ссылку сами не перехватывают: из магазина
    # человек с приложением не попал бы на место, а со страницы — попадёт
    monkeypatch.setattr(settings, "ios_store_redirect", False)
    r = await _open(client, IPHONE)
    assert r.status_code == 200
    assert 'href="sayr://place/test-waterfall"' in r.text


async def test_android_chrome_открывает_приложение_а_без_него_google_play(client, stores):
    r = await _open(client, CHROME)
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith(
        "intent://place/test-waterfall#Intent;scheme=sayr;package=uz.sayr.android;"
    )
    assert location.endswith(";end")
    fallback = location.split("S.browser_fallback_url=")[1].split(";")[0]
    assert unquote(fallback) == "https://sayr.info/go/android?from=share"


async def test_встроенный_браузер_android_сразу_в_google_play(client, stores):
    r = await _open(client, WEBVIEW)
    assert r.status_code == 302
    assert r.headers["location"] == "https://sayr.info/go/android?from=share"


@pytest.mark.parametrize("ua", [TELEGRAM, IMESSAGE, GOOGLEBOT, DESKTOP, ""])
async def test_роботы_превью_и_компьютер_видят_страницу(client, stores, ua):
    r = await _open(client, ua)
    assert r.status_code == 200
    assert 'property="og:title"' in r.text
    assert "https://sayr.info/go/ios?from=share" in r.text
    assert "https://sayr.info/go/android?from=share" in r.text
    # iPad представляется компьютером: ему баннер Safari «Открыть / Загрузить»
    assert (
        '<meta name="apple-itunes-app" content="app-id=6800192455, '
        'app-argument=https://sayr.info/p/test-waterfall">'
    ) in r.text
    assert r.headers["vary"] == "User-Agent"


async def test_без_адресов_магазинов_телефон_видит_страницу(client, monkeypatch):
    monkeypatch.setattr(settings, "app_store_url", "")
    monkeypatch.setattr(settings, "play_store_url", "")
    for ua in (IPHONE, CHROME):
        r = await _open(client, ua)
        assert r.status_code == 200
        assert "apple-itunes-app" not in r.text
        assert "/go/" not in r.text


async def test_несуществующее_место_в_магазин_не_уводит(client, stores):
    r = await _open(client, IPHONE, slug="net-takogo-mesta")
    assert r.status_code == 404


async def test_переход_в_магазин_записывается_с_меткой(client, stores):
    r = await client.get("/go/ios?from=share", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == STORE
