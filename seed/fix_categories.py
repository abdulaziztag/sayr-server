"""Чинит категорию у мест, где она противоречит собственному описанию.

    uv run python -m seed.fix_categories            # показать
    uv run python -m seed.fix_categories --apply    # записать

Категория подтянулась при импорте по имени точки, а не по смыслу
объекта, и разошлась с текстом карточки: «Петроглифы Каракии» числятся
вершиной, «Коксуйская щель» — озером.

Раньше это была одна плашка из трёх, теперь категория — половина
подписи на карточке каталога («ПИК · УГАМ»), и врёт она заметно.
Категория ещё и выбирает маску кадра и участвует в фильтре, так что
ошибка стоит дороже одной строки.

Скрипт разовый, но идемпотентный: место с уже верной категорией
пропускается. Правится и `seed/data/new_places.json`, иначе повторный
импорт вернул бы прежнее значение.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place, PlaceCategory

#: slug → (верная категория, чем подтверждается)
FIXES = {
    "korakiya-petrogliflari": (
        PlaceCategory.petroglyphs,
        "«Наскальные рисунки над Каракиясаем» — петроглифы, а не вершина",
    ),
    "kuk-suv": (
        PlaceCategory.gorge,
        "«река зажата высокими скалами», щель — ущелье, а не озеро",
    ),
}


async def run(apply: bool) -> None:
    fixed = 0
    async with SessionLocal() as session:
        for slug, (category, why) in FIXES.items():
            place = (
                await session.execute(select(Place).where(Place.slug == slug))
            ).scalar_one_or_none()
            if place is None:
                print(f"  ! {slug}: места нет")
                continue
            if place.category == category:
                print(f"  = {slug}: уже {category.value}")
                continue
            print(f"  {slug:26} {place.category.value:12} → {category.value}")
            print(f"    {why}")
            fixed += 1
            if apply:
                place.category = category

        print(f"\nчинится {fixed} из {len(FIXES)}")
        if apply:
            await session.commit()
            print("Записано.")
        else:
            print("Показ без изменений. Выполнить: --apply")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
