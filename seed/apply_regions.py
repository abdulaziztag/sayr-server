"""Переразметить каталог по регионам: seed/data/regions_map.json.

    uv run python -m seed.apply_regions data/regions_map.json          # показать
    uv run python -m seed.apply_regions data/regions_map.json --apply  # записать

Прежние регионы смешивали три разные системы: урочища («Чимган–Чарвак»,
«Угам-Чаткал»), районы («Ахангаран», «Нурата») и области («Кашкадарья»,
«Самарканд»). Из-за этого восемь мест числились не в той области —
Арашан с Зикркулем стояли в «Чимган–Чарваке», а лежат за Чаткальским
хребтом, в Наманганской.

Теперь район берётся из OpenStreetMap по координатам места, а Бостанлыкский
(54 места, почти половина каталога) дробится по долинам — правило записано
в самом файле, ключ `_rule`.

Порядок операций жёсткий и обратный привычному:

1. создать недостающие регионы — `Region.name` UNIQUE, и пока старые живы,
   имена не конфликтуют;
2. переставить места;
3. удалить опустевшие — но **только пустые**: регион, в котором кто-то
   остался, означает дыру в сопоставлении, и снести его молча значило бы
   потерять места;
4. проставить порядок.

Порядок регионов — по удалённости от Ташкента, а не по алфавиту: список
в фильтре сам подсказывает, что рядом, а что на выходные с ночёвкой.
"""

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place, Region

#: Узбекские названия регионов. Держим здесь, а не в файле данных: файл
#: отвечает на вопрос «какое место где», а это — справочник самих регионов
NAME_UZ = {
    "Чимган": "Chimyon",
    "Чарвак": "Chorvoq",
    "Угам": "Ugom",
    "Паркент": "Parkent",
    "Пскем": "Pskom",
    "Ахангаран": "Ohangaron",
    "Наманган": "Namangan",
    "Джизак": "Jizzax",
    "Самарканд": "Samarqand",
    "Кашкадарья": "Qashqadaryo",
    "Навои": "Navoiy",
    "Сурхандарья": "Surxondaryo",
}


async def run(path: Path, apply: bool) -> None:
    data = json.loads(path.read_text("utf-8"))
    wanted: dict[str, str] = data["places"]
    order: list[str] = data["_order"]

    missing_uz = set(order) - set(NAME_UZ)
    if missing_uz:
        raise SystemExit(f"нет узбекских названий для: {', '.join(sorted(missing_uz))}")

    async with SessionLocal() as session:
        regions = {
            r.name: r for r in (await session.execute(select(Region))).scalars().all()
        }

        # 1. Недостающие регионы — до переноса: имя уникально, и старые
        #    ещё занимают свои
        for index, name in enumerate(order):
            if name in regions:
                continue
            print(f"  + регион {name}")
            region = Region(name=name, name_uz=NAME_UZ[name], sort_order=index)
            session.add(region)
            regions[name] = region
        if apply:
            await session.flush()

        # 2. Места
        places = (await session.execute(select(Place))).scalars().all()
        by_id = {r.id: n for n, r in regions.items()}

        #: Куда каждое место встанет после переноса. Место, которого нет
        #: в файле, остаётся где было — каталог мог уйти вперёд файла
        after: Counter[str] = Counter()
        moved = 0
        for place in sorted(places, key=lambda p: p.slug):
            was = by_id.get(place.region_id, "?")
            target = wanted.get(place.slug, was)
            after[target] += 1
            if target == was:
                continue
            moved += 1
            print(f"  {place.slug:34} {was:16} → {target}")
            if apply:
                place.region_id = regions[target].id

        known = {p.slug for p in places}
        missing = sorted(set(wanted) - known)

        if apply:
            await session.flush()

        # 3. Опустевшие — только они. Остаток считаем по будущему
        #    состоянию, иначе показ без --apply ругался бы на каждый
        #    старый регион: места-то ещё не переехали
        for name, region in list(regions.items()):
            if name in order:
                continue
            left = after[name]
            if left:
                print(f"  ! {name}: остаётся {left} мест — не удаляю, проверьте сопоставление")
                continue
            print(f"  − регион {name}")
            if apply:
                await session.delete(region)

        # 4. Порядок
        for index, name in enumerate(order):
            if apply:
                regions[name].sort_order = index
                regions[name].name_uz = regions[name].name_uz or NAME_UZ[name]

        tail = ""
        if missing:
            shown = ", ".join(missing[:6])
            more = f" и ещё {len(missing) - 6}" if len(missing) > 6 else ""
            tail = f", нет в базе {len(missing)}: {shown}{more}"
        print(
            f"\nрегионов {len(order)}, мест в базе {len(places)}, "
            f"переезжает {moved}{tail}"
        )
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
