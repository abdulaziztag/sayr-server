"""Разложить написанные карточки по черновикам: имя, описание, как добраться.

    uv run python -m seed.apply_cards data/place_cards.json            # показать
    uv run python -m seed.apply_cards data/place_cards.json --apply    # записать
    uv run python -m seed.apply_cards data/channel_cards.json --slug zamok --apply

Файл — список {slug, name, short_desc, description_md, how_to_get_md}.
Тексты написаны по цитатам форума «ГОРЕЦ» и данным tabiatsari; раздел
«Доступ» в конце описания несёт даты и факты об ограничениях.

Обновляются ТОЛЬКО черновики (is_published = False): у опубликованных
мест тексты либо написаны руками, либо уже вычитаны — скрипт им не судья.
Публикация остаётся за человеком в админке.

С --slug берутся только названные места — и опубликованные тоже: раз
человек назвал место явно, он знает, что правит.
"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place


async def run(path: Path, apply: bool, slugs: list[str] | None = None) -> None:
    cards = json.loads(path.read_text("utf-8"))
    if slugs:
        cards = [c for c in cards if c["slug"] in slugs]
        missing = set(slugs) - {c["slug"] for c in cards}
        if missing:
            raise SystemExit(f"в файле нет карточек: {sorted(missing)}")
    updated = skipped = 0

    async with SessionLocal() as session:
        for card in cards:
            place = (
                await session.execute(select(Place).where(Place.slug == card["slug"]))
            ).scalar_one_or_none()
            if place is None:
                print(f"  ! {card['slug']}: места нет")
                continue
            if place.is_published and not slugs:
                print(f"  ~ {card['slug']}: опубликовано, не трогаем")
                skipped += 1
                continue

            updated += 1
            renamed = f" (было «{place.name}»)" if place.name != card["name"] else ""
            access = "  [доступ]" if "**Доступ:**" in card["description_md"] else ""
            howto = "" if card["how_to_get_md"].strip() else "  [без «как добраться»]"
            print(f"  {card['name'][:28]:28}{renamed}{access}{howto}")
            if not apply:
                continue
            place.name = card["name"]
            place.short_desc = card["short_desc"]
            place.description_md = card["description_md"]
            place.how_to_get_md = card["how_to_get_md"]

        if apply:
            await session.commit()
            print(f"\nГотово: обновлено {updated}, пропущено опубликованных {skipped}.")
        else:
            print(f"\nПоказ без изменений: обновится {updated}. Выполнить: --apply")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    parser.add_argument(
        "--slug", action="append", default=[], metavar="SLUG",
        help="только это место, даже если опубликовано (можно несколько раз)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.file, args.apply, args.slug))


if __name__ == "__main__":
    main()
