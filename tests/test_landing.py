"""Лендинг: метка канала в кнопках магазинов и редирект /go/… с учётом клика."""

from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.models import ApiEvent

STORE = "https://apps.apple.com/app/id6800192455"
PLAY = "https://play.google.com/store/apps/details?id=uz.sayr.android"


async def _clear():
    async with SessionLocal() as session:
        await session.execute(delete(ApiEvent))
        await session.commit()


async def _events(kind: str):
    async with SessionLocal() as session:
        return (
            await session.execute(select(ApiEvent).where(ApiEvent.kind == kind))
        ).scalars().all()


async def test_go_ios_records_click_with_mark_and_redirects(client, monkeypatch):
    await _clear()
    monkeypatch.setattr(settings, "app_store_url", STORE)
    resp = await client.get("/go/ios?from=gorets", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == STORE
    clicks = await _events("store_ios")
    assert [c.slug for c in clicks] == ["gorets"]
    assert clicks[0].device is None


async def test_go_android_without_mark_writes_empty_key(client, monkeypatch):
    await _clear()
    monkeypatch.setattr(settings, "play_store_url", PLAY)
    resp = await client.get("/go/android", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == PLAY
    assert [c.slug for c in await _events("store_android")] == [None]


async def test_go_cleans_hostile_mark(client, monkeypatch):
    await _clear()
    monkeypatch.setattr(settings, "app_store_url", STORE)
    await client.get("/go/ios?from=%3Cscript%3Ex%3C/script%3E", follow_redirects=False)
    assert [c.slug for c in await _events("store_ios")] == ["scriptxscript"]


async def test_go_without_store_url_falls_back_to_landing(client, monkeypatch):
    await _clear()
    monkeypatch.setattr(settings, "play_store_url", "")
    resp = await client.get("/go/android?from=work", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "/"
    # Клик всё равно посчитан: человек хотел в магазин
    assert [c.slug for c in await _events("store_android")] == ["work"]


async def test_go_unknown_platform_is_404(client):
    resp = await client.get("/go/windows", follow_redirects=False)
    assert resp.status_code == 404


async def test_landing_threads_mark_into_store_buttons(client, monkeypatch):
    monkeypatch.setattr(settings, "app_store_url", STORE)
    monkeypatch.setattr(settings, "play_store_url", PLAY)
    page = (await client.get("/?from=lider")).text
    assert 'href="/go/ios?from=lider"' in page
    assert 'href="/go/android?from=lider"' in page
    assert STORE not in page and PLAY not in page  # прямых ссылок в магазин больше нет


async def test_landing_without_mark_has_plain_go_links(client, monkeypatch):
    monkeypatch.setattr(settings, "app_store_url", STORE)
    monkeypatch.setattr(settings, "play_store_url", PLAY)
    page = (await client.get("/uz")).text
    assert 'href="/go/ios"' in page and 'href="/go/android"' in page


async def test_landing_without_play_keeps_tester_anchor(client, monkeypatch):
    """Пока Play не открыт, андроидная кнопка ведёт к форме теста, а не в /go."""
    monkeypatch.setattr(settings, "app_store_url", STORE)
    monkeypatch.setattr(settings, "play_store_url", "")
    page = (await client.get("/?from=gorets")).text
    assert 'href="#android"' in page
    assert 'href="/go/ios?from=gorets"' in page
