"""Переименовать места по карте slug → имя (ru и uz).

    uv run python -m seed.rename_places data/rename_type_prefix.json          # показать
    uv run python -m seed.rename_places data/rename_type_prefix.json --apply  # записать

Зачем отдельный скрипт: `apply_cards` пропускает опубликованные места, а
переименовать надо именно их. Первая карта — снятие типа из названий
(«Водопад Акташ» → «Акташ»): тип и так стоит в мета-строке карточки, а в
имени он дублировал её на половине каталога (решение владельца 2026-09-02).

Файл — список объектов {"slug", "name", "name_uz"}; пустой name_uz не трогает
перевод. Перед --apply на проде — бэкап данных:
    sudo -n -u postgres pg_dump sayr | gzip > /var/backups/sayr/pre-rename.sql.gz
Клиенты подхватят имена при следующей загрузке каталога; снимки карточек в
избранном и планах освежаются по slug, напоминания перепланируются.
"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place


async def run(path: Path, apply: bool) -> None:
    rows = json.loads(path.read_text("utf-8"))
    changed = missing = 0
    async with SessionLocal() as session:
        for row in rows:
            place = (
                await session.execute(select(Place).where(Place.slug == row["slug"]))
            ).scalar_one_or_none()
            if place is None:
                print(f"  ?  {row['slug']}: нет в базе")
                missing += 1
                continue
            new_ru = row["name"].strip()
            new_uz = (row.get("name_uz") or "").strip() or None
            marks = []
            if place.name != new_ru:
                marks.append(f"«{place.name}» → «{new_ru}»")
            if new_uz and place.name_uz != new_uz:
                marks.append(f"uz «{place.name_uz}» → «{new_uz}»")
            if not marks:
                continue
            changed += 1
            print(f"  *  {row['slug']:26} " + " ; ".join(marks))
            if apply:
                place.name = new_ru
                if new_uz:
                    place.name_uz = new_uz
        if apply:
            await session.commit()
    verb = "записано" if apply else "к записи"
    print(f"\n{verb}: {changed} мест, не найдено: {missing}" + ("" if apply else "  (добавьте --apply)"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path)
    parser.add_argument("--apply", action="store_true", help="записать в базу (без флага — только показать)")
    args = parser.parse_args()
    asyncio.run(run(args.path, args.apply))


if __name__ == "__main__":
    main()
