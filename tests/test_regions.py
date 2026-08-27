"""Регионы: порядок выдачи и защита переразметки.

Порядок здесь не косметика. Регионы отдаются по `sort_order`, который
задан по удалённости от Ташкента, — список в фильтре сам подсказывает,
что рядом, а что на выходные с ночёвкой. Алфавит эту подсказку убил бы:
«Сурхандарья» встала бы прежде «Чимгана».

Отдельно проверяется главная защита скрипта переразметки: регион,
в котором после переноса кто-то остался, удалять нельзя. Такой остаток
означает дыру в сопоставлении, и снести его молча значило бы потерять
места.
"""

from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Place, PlaceCategory, Region
from seed.apply_regions import NAME_UZ, run
from seed.fix_categories import run as fix_categories


async def test_regions_go_by_sort_order_not_alphabet(client):
    """«Дальний» стоит вторым, хотя по алфавиту он первый."""
    names = [r["name"] for r in (await client.get("/api/v1/regions")).json()]
    assert names == ["Тестовый регион", "Дальний регион"]
    assert names != sorted(names)


async def test_every_place_has_a_region(client):
    async with SessionLocal() as session:
        orphans = (
            await session.execute(
                select(func.count()).select_from(Place).where(Place.region_id.is_(None))
            )
        ).scalar_one()
    assert orphans == 0


async def test_no_region_is_empty(client):
    """Пустой регион в фильтре — строка, которая ничего не находит."""
    for region in (await client.get("/api/v1/regions")).json():
        assert region["places_count"] > 0, region["name"]


async def test_sort_order_is_unique(client):
    async with SessionLocal() as session:
        orders = (await session.execute(select(Region.sort_order))).scalars().all()
    assert len(orders) == len(set(orders))


async def test_shipped_map_covers_every_region_name(tmp_path):
    """У каждого региона из файла есть узбекское имя — иначе скрипт встанет."""
    data = Path("seed/data/regions_map.json")
    payload = __import__("json").loads(data.read_text("utf-8"))
    assert set(payload["_order"]) <= set(NAME_UZ)
    assert set(payload["places"].values()) == set(payload["_order"])


async def test_remap_refuses_to_drop_a_region_with_places(client, tmp_path, capsys):
    """Регион с остатком не удаляется — даже если его нет в новой схеме.

    Сопоставление намеренно неполное: «Дальний регион» в нём не упомянут
    вовсе, а его место не переезжает. Скрипт обязан оставить регион
    на месте и сказать об этом вслух.
    """
    partial = tmp_path / "regions.json"
    partial.write_text(
        __import__("json").dumps(
            {
                "_order": ["Тестовый регион"],
                "places": {"test-waterfall": "Тестовый регион"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(NAME_UZ, "Тестовый регион", "Test viloyati")
        await run(partial, apply=True)

    out = capsys.readouterr().out
    assert "не удаляю" in out

    async with SessionLocal() as session:
        survived = (
            await session.execute(select(Region).where(Region.name == "Дальний регион"))
        ).scalar_one_or_none()
    assert survived is not None, "регион с местом внутри снесли"


async def test_category_fix_is_idempotent(client):
    """Второй прогон ничего не трогает — скрипт разовый, но живёт в репозитории."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "seed.fix_categories.FIXES",
            {"test-lake": (PlaceCategory.gorge, "проверка")},
        )
        await fix_categories(apply=True)
        await fix_categories(apply=True)

    async with SessionLocal() as session:
        place = (
            await session.execute(select(Place).where(Place.slug == "test-lake"))
        ).scalar_one()
    assert place.category == PlaceCategory.gorge
