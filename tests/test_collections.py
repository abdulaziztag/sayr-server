"""Коллекции «Проекта 21»: карта — единственный источник правды.

Проверяется главное обещание скрипта: без --apply ничего не пишется,
без --with-mock мок не попадает в базу, место вне карты обнуляется,
а неизвестный slug останавливает прогон до первой записи. Тесты
самостоятельны: каждый заканчивает пустой картой.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place
from seed.apply_collections import run


async def _slugs() -> list[str]:
    async with SessionLocal() as session:
        return list((await session.execute(select(Place.slug).order_by(Place.slug))).scalars())


async def _collections(slug: str) -> list[str]:
    async with SessionLocal() as session:
        value = (await session.execute(select(Place.collections).where(Place.slug == slug))).scalar_one()
        return list(value or [])


def _map(tmp_path: Path, real: list[str], mock: list[str], code: str = "cascade") -> Path:
    path = tmp_path / "collections.json"
    path.write_text(json.dumps({"_note": "тест", code: {"from_channel": real, "mock": mock}}), "utf-8")
    return path


async def _reset(tmp_path: Path) -> None:
    await run(_map(tmp_path, [], []), apply=True)


async def test_dry_run_lists_but_writes_nothing(tmp_path, capsys):
    a, b = (await _slugs())[:2]
    await run(_map(tmp_path, [a], [b]), apply=False, with_mock=True)
    out = capsys.readouterr().out
    assert a in out and b in out and "к записи: 2" in out
    assert await _collections(a) == [] and await _collections(b) == []


async def test_apply_skips_mock_without_flag(tmp_path):
    a, b = (await _slugs())[:2]
    await run(_map(tmp_path, [a], [b]), apply=True)
    assert await _collections(a) == ["cascade"]
    assert await _collections(b) == []
    await _reset(tmp_path)
    assert await _collections(a) == []


async def test_with_mock_writes_mock_and_map_is_the_truth(tmp_path):
    a, b, c = (await _slugs())[:3]
    await run(_map(tmp_path, [a], [b]), apply=True, with_mock=True)
    assert await _collections(a) == ["cascade"] and await _collections(b) == ["cascade"]
    # место убрали из карты — коллекции у него не остаётся
    await run(_map(tmp_path, [c], []), apply=True)
    assert await _collections(a) == [] and await _collections(b) == [] and await _collections(c) == ["cascade"]
    await _reset(tmp_path)


async def test_unknown_slug_stops_before_write(tmp_path):
    a = (await _slugs())[0]
    with pytest.raises(SystemExit):
        await run(_map(tmp_path, [a, "no-such-place"], []), apply=True)
    assert await _collections(a) == []


async def test_unknown_collection_code_rejected(tmp_path):
    a = (await _slugs())[0]
    with pytest.raises(SystemExit):
        await run(_map(tmp_path, [a], [], code="rainbow"), apply=False)


def test_shipped_map_is_consistent():
    payload = json.loads((Path(__file__).resolve().parents[1] / "seed" / "data" / "collections.json").read_text("utf-8"))
    codes = [k for k in payload if not k.startswith("_")]
    assert codes == ["cascade", "horizon", "mirage", "underground"]
    for code in codes:
        real, mock = payload[code]["from_channel"], payload[code]["mock"]
        assert real and not set(real) & set(mock)
        assert len(real) + len(mock) == 8
