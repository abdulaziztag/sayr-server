"""Разложить коллекции «Проекта 21» по местам: seed/data/collections.json.

    uv run python -m seed.apply_collections seed/data/collections.json               # показать
    uv run python -m seed.apply_collections seed/data/collections.json --apply       # записать
    uv run python -m seed.apply_collections seed/data/collections.json --apply --with-mock

Файл — по ключу на коллекцию (`cascade`, `horizon`, `mirage`, `underground`),
в каждом два списка slug: `from_channel` — выведено из хэштегов канала,
`mock` — временное дополнение для локальной разработки. Без `--with-mock`
мок не пишется: чужие люди не должны увидеть «входит в 21 подземелье»
у пещеры, которую владелец туда не вносил.

Карта — единственный источник правды: массив `collections` перезаписывается
целиком, место вне карты получает пусто. Опубликованные места не
пропускаются (как `apply_translations`). Неизвестный slug — ошибка до записи.
"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place

CODES = ("cascade", "horizon", "mirage", "underground")


def targets(data: dict, with_mock: bool) -> dict[str, set[str]]:
    """slug → коды коллекций по файлу."""
    result: dict[str, set[str]] = {}
    for code, lists in data.items():
        if code.startswith("_"):
            continue
        if code not in CODES:
            raise SystemExit(f"неизвестная коллекция: {code} (ждём {', '.join(CODES)})")
        slugs = list(lists.get("from_channel", []))
        if with_mock:
            slugs += list(lists.get("mock", []))
        for slug in slugs:
            result.setdefault(slug, set()).add(code)
    return result


async def run(path: Path, apply: bool, with_mock: bool = False) -> None:
    data = json.loads(path.read_text("utf-8"))
    wanted = targets(data, with_mock)
    changed = 0
    async with SessionLocal() as session:
        places = (await session.execute(select(Place))).scalars().all()
        known = {p.slug for p in places}
        unknown = sorted(set(wanted) - known)
        if unknown:
            raise SystemExit(f"нет в базе: {', '.join(unknown)}")
        for place in sorted(places, key=lambda p: p.slug):
            new = sorted(wanted.get(place.slug, set()), key=CODES.index)
            old = sorted(place.collections or [])
            if old == sorted(new):
                continue
            changed += 1
            print(f"  *  {place.slug:26} {', '.join(old) or '—':22} → {', '.join(new) or '—'}")
            if apply:
                place.collections = new
        if apply:
            await session.commit()
    verb = "записано" if apply else "к записи"
    mock = " (с моком)" if with_mock else ""
    print(f"\n{verb}: {changed} мест{mock}" + ("" if apply else "  (добавьте --apply)"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path)
    parser.add_argument("--apply", action="store_true", help="записать в базу (без флага — только показать)")
    parser.add_argument("--with-mock", action="store_true", help="добавить временный мок из раздела mock")
    args = parser.parse_args()
    asyncio.run(run(args.path, args.apply, args.with_mock))


if __name__ == "__main__":
    main()
