"""Переразметить каталог по сложности: seed/data/difficulty_map.json.

    uv run python -m seed.apply_difficulty data/difficulty_map.json          # показать
    uv run python -m seed.apply_difficulty data/difficulty_map.json --apply  # записать

Прежние три ступени расставлялись по времени и набору высоты и потому
повторяли наклейки с километрами и часами. Больше половины каталога —
67 мест из 121 — осело в «средне», и метка перестала различать.

Теперь ступень отвечает на то, чего числа не говорят: насколько дорого
стоит ошибка. Правило записано в самом файле, ключ `_rule`; обоснование
у каждого места — в `why`, и по нему разметку вычитывают.

Заодно проставляется `trip_days` — только там, где в базе уже стоит
`overnight`. Ночёвку скрипт не выдумывает: место, которое форум называет
многодневным, но у которого ночёвки в базе нет, он покажет отдельно
и оставит как есть. Проставить её — решение владельца, а не скрипта:
от `overnight` зависит и окно выезда, и наклейка на карточке.
"""

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Difficulty, Place

ORDER = [Difficulty.easy, Difficulty.medium, Difficulty.hard, Difficulty.extreme]


async def run(path: Path, apply: bool) -> None:
    data = json.loads(path.read_text("utf-8"))
    wanted: dict[str, dict] = data["places"]
    days: dict[str, int] = data.get("trip_days", {})

    unknown = {
        slug: entry["level"]
        for slug, entry in wanted.items()
        if entry["level"] not in Difficulty.__members__
    }
    if unknown:
        raise SystemExit(f"неизвестные ступени: {unknown}")

    async with SessionLocal() as session:
        places = {
            p.slug: p for p in (await session.execute(select(Place))).scalars().all()
        }

        moved = 0
        after: Counter[Difficulty] = Counter()
        for slug, place in sorted(places.items()):
            entry = wanted.get(slug)
            if entry is None:
                after[place.difficulty] += 1
                continue
            target = Difficulty[entry["level"]]
            after[target] += 1
            if place.difficulty is not target:
                moved += 1
                print(f"  {slug:30} {place.difficulty.value:8} → {target.value}")
                print(f"    {entry['why']}")
                if apply:
                    place.difficulty = target

        # Дни — только к уже проставленной ночёвке
        for slug, count in sorted(days.items()):
            place = places.get(slug)
            if place is None:
                print(f"  ! {slug}: места нет")
                continue
            if place.overnight is None:
                print(f"  ! {slug}: дней {count}, но ночёвки в базе нет — пропускаю")
                continue
            if place.trip_days == count:
                continue
            print(f"  {slug:30} дней: {place.trip_days} → {count}")
            if apply:
                place.trip_days = count

        # Ночёвка есть, а дней нет — наклейка скажет «НОЧЁВКА» вместо числа
        silent = [
            slug
            for slug, p in sorted(places.items())
            if p.overnight is not None and slug not in days
        ]
        for slug in silent:
            print(f"  ~ {slug}: ночёвка есть, дней в файле нет — останется «ночёвка»")

        missing = sorted(set(wanted) - set(places))
        print(
            f"\nмест в базе {len(places)}, в файле {len(wanted)}, "
            f"меняет ступень {moved}"
            + (f", нет в базе {len(missing)}: {', '.join(missing[:6])}" if missing else "")
        )
        print("после переразметки: " + " · ".join(f"{d.value} {after[d]}" for d in ORDER))

        if apply:
            await session.commit()
            print("Записано.")
        else:
            print("Показ без изменений. Выполнить: --apply")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    args = parser.parse_args()
    asyncio.run(run(args.file, args.apply))


if __name__ == "__main__":
    main()
