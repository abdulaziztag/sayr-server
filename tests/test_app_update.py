"""Порог версии приложения: флаг принудительного обновления по платформам."""

import pytest
from sqlalchemy import delete

from app.api.app_update import parse_version
from app.db import SessionLocal
from app.models import AppUpdate


async def _set(platform: str, min_version: str, force: bool) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(AppUpdate).where(AppUpdate.platform == platform))
        session.add(AppUpdate(platform=platform, min_version=min_version, force=force))
        await session.commit()


@pytest.fixture(autouse=True)
async def clean():
    yield
    async with SessionLocal() as session:
        await session.execute(delete(AppUpdate))
        await session.commit()


def test_versions_compare_as_numbers_not_strings():
    assert parse_version("1.10.0") > parse_version("1.9.0")
    assert parse_version("1.2") == parse_version("1.2.0")
    assert parse_version("1.2.3-beta") == (1, 2, 3)
    assert parse_version("garbage") == (0, 0, 0)


async def test_no_row_means_no_pressure(client):
    resp = await client.get("/api/v1/app/update", params={"platform": "ios", "version": "1.0.0"})
    assert resp.status_code == 200
    assert resp.json()["force"] is False


async def test_flag_hits_only_versions_below_threshold(client):
    await _set("android", "1.3.0", force=True)
    old = await client.get("/api/v1/app/update", params={"platform": "android", "version": "1.2.9"})
    same = await client.get("/api/v1/app/update", params={"platform": "android", "version": "1.3.0"})
    newer = await client.get("/api/v1/app/update", params={"platform": "android", "version": "1.10.0"})
    assert old.json()["force"] is True and old.json()["min_version"] == "1.3.0"
    assert same.json()["force"] is False
    assert newer.json()["force"] is False
    # Другая платформа своего порога не имеет
    ios = await client.get("/api/v1/app/update", params={"platform": "ios", "version": "0.1"})
    assert ios.json()["force"] is False


async def test_threshold_without_flag_is_silent(client):
    await _set("ios", "9.9.9", force=False)
    resp = await client.get("/api/v1/app/update", params={"platform": "ios", "version": "1.0.0"})
    assert resp.json() == {"force": False, "min_version": "9.9.9", "store_url": None}


async def test_unknown_platform_is_rejected(client):
    resp = await client.get("/api/v1/app/update", params={"platform": "windows", "version": "1.0.0"})
    assert resp.status_code == 422
