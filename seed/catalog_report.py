"""Инвентаризация каталога: что готово, чего не хватает, что проверить.

    uv run python -m seed.catalog_report > ../docs/catalog-status.md

Отчёт для человека, который вычитывает каталог: по каждому месту видно,
каких данных нет и почему это важно. Считается из базы, ничего не меняет.
"""

import asyncio
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models import Place

# Дорога туда-обратно плюс ход с запасом должны уместиться в световой день.
# Больше — и окно выезда уходит в минус: приложение не сможет сказать,
# когда выезжать, потому что за день туда не обернуться
DAY_BUDGET_HOURS = 14.0


def _needs(place: Place) -> list[str]:
    out = []
    if not place.photos:
        out.append("фото")
    if not place.tracks:
        out.append("трек")
    if place.distance_km is None:
        out.append("длина")
    if place.duration_hours is None:
        out.append("время")
    if place.elevation_gain_m is None:
        out.append("набор")
    if place.elevation_m is None:
        out.append("высота")
    if len(place.description_md or "") < 40:
        out.append("описание")
    if len(place.how_to_get_md or "") < 20:
        out.append("как добраться")
    if not place.best_seasons:
        out.append("сезон")
    return out


def _window_hours(place: Place) -> float | None:
    """Сколько часов светового дня нужно на выход целиком."""
    if place.drive_minutes is None:
        return None
    walk = place.duration_hours or 0
    return place.drive_minutes * 2 / 60 + walk * 1.5


async def run() -> None:
    async with SessionLocal() as session:
        places = (
            (
                await session.execute(
                    select(Place)
                    .options(
                        selectinload(Place.photos),
                        selectinload(Place.tracks),
                        selectinload(Place.region),
                    )
                    .order_by(Place.name)
                )
            )
            .unique()
            .scalars()
            .all()
        )

    pub = [p for p in places if p.is_published]
    drafts = [p for p in places if not p.is_published]
    print("# Каталог Sayr — что готово и что проверить\n")
    print(f"Собрано из базы. Всего мест **{len(places)}**: "
          f"опубликовано {len(pub)}, черновиков {len(drafts)}.\n")

    print("## Полнота данных\n")
    print("| Что | Есть | Нет |")
    print("|---|---:|---:|")
    checks = [
        ("Фотографии", lambda p: bool(p.photos)),
        ("Трек", lambda p: bool(p.tracks)),
        ("Длина маршрута", lambda p: p.distance_km is not None),
        ("Время в пути", lambda p: p.duration_hours is not None),
        ("Набор высоты", lambda p: p.elevation_gain_m is not None),
        ("Высота места", lambda p: p.elevation_m is not None),
        ("Дорога от Ташкента", lambda p: p.drive_minutes is not None),
        ("Описание", lambda p: len(p.description_md or "") >= 40),
        ("Как добраться", lambda p: len(p.how_to_get_md or "") >= 20),
        ("Сезон", lambda p: bool(p.best_seasons)),
    ]
    for label, ok in checks:
        n = sum(1 for p in places if ok(p))
        print(f"| {label} | {n} | {len(places) - n} |")

    # Места, куда за день не обернуться: приложение обещает окно выезда,
    # которого физически нет
    far = [(p, h) for p in places if (h := _window_hours(p)) and h > DAY_BUDGET_HOURS]
    print(f"\n## Не однодневные выходы — {len(far)}\n")
    print("Дорога туда-обратно плюс ход не помещаются в световой день. "
          "Приложение считает окно выезда по формуле «закат минус дорога и ход», "
          "и для этих мест оно уходит в минус — окна не будет ни при каком закате. "
          "Им нужна пометка ночёвки, либо их стоит держать черновиками.\n")
    print("| Место | Дорога | Ход | Нужно часов | Статус |")
    print("|---|---:|---:|---:|---|")
    for p, h in sorted(far, key=lambda x: -x[1]):
        drive = f"{p.drive_minutes // 60}:{p.drive_minutes % 60:02}"
        walk = f"{p.duration_hours} ч" if p.duration_hours else "—"
        status = "опубликовано" if p.is_published else "черновик"
        print(f"| {p.name} | {drive} | {walk} | {h:.0f} | {status} |")

    print(f"\n## Опубликованные с пропусками\n")
    holes = [(p, _needs(p)) for p in pub if _needs(p)]
    print(f"Видны людям прямо сейчас — {len(holes)} из {len(pub)}.\n")
    print("| Место | Регион | Чего нет |")
    print("|---|---|---|")
    for p, need in sorted(holes, key=lambda x: (-len(x[1]), x[0].name)):
        print(f"| {p.name} | {p.region.name} | {', '.join(need)} |")

    print(f"\n## Черновики — {len(drafts)}\n")
    ready = [p for p in drafts if not _needs(p)]
    print(f"Полностью готовы к публикации: **{len(ready)}**.\n")
    print("| Место | Регион | Фото | Трек | Чего не хватает |")
    print("|---|---|---:|---:|---|")
    for p in drafts:
        need = _needs(p)
        print(f"| {p.name} | {p.region.name} | {len(p.photos)} | {len(p.tracks)} | "
              f"{', '.join(need) if need else '— готово'} |")

    print("\n## По регионам\n")
    by_region = Counter(p.region.name for p in places)
    pub_region = Counter(p.region.name for p in pub)
    print("| Регион | Всего | Опубликовано |")
    print("|---|---:|---:|")
    for region, total in by_region.most_common():
        print(f"| {region} | {total} | {pub_region.get(region, 0)} |")


if __name__ == "__main__":
    asyncio.run(run())
