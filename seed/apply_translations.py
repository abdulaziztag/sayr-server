"""Разложить узбекские переводы по каталогу: места, регионы, треки.

    uv run python -m seed.apply_translations data/translations_uz.json          # показать
    uv run python -m seed.apply_translations data/translations_uz.json --apply  # записать

Файл — объект с тремя разделами, любой можно опустить:

    {
      "regions": {"Чимган–Чарвак": "Chimyon–Charvoq", ...},
      "places":  {"azadbash": {"name_uz": ..., "short_desc_uz": ..., ...}, ...},
      "tracks":  {"По водопадам Азадбаша": "Ozodbosh sharsharalari boʻylab", ...}
    }

Ключ мест — slug, и только он. Русские имена в базе разошлись с транслитерацией
у большинства записей, а slug неизменен — на нём же стоят ссылки `/p/{slug}`
и имена файлов. Регионы и треки сопоставляются по русскому имени: своего
устойчивого ключа у них нет, зато имена уникальны в пределах каталога.

В отличие от `apply_cards`, опубликованные места НЕ пропускаются: переводить
надо именно их — черновиков в каталоге пять из ста двадцати шести.

Пустая строка в переводе означает «перевода нет» и записывается как NULL:
то же самое различие поддерживает фолбэк в `schemas.pick`. Это позволяет
снять ошибочный перевод, не открывая админку.
"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place, PlaceTrack, Region

PLACE_FIELDS = ["name_uz", "short_desc_uz", "description_md_uz", "how_to_get_md_uz"]


def _clean(value: str | None) -> str | None:
    """Пустое и пробельное — это NULL, а не пустая строка."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _diff_mark(before: str | None, after: str | None) -> str:
    if before == after:
        return "="
    if before is None:
        return "+"
    return "-" if after is None else "*"


async def run(path: Path, apply: bool) -> None:
    data = json.loads(path.read_text("utf-8"))
    stats = {"regions": 0, "places": 0, "fields": 0, "tracks": 0, "missing": 0}

    async with SessionLocal() as session:
        for ru_name, uz_name in (data.get("regions") or {}).items():
            region = (
                await session.execute(select(Region).where(Region.name == ru_name))
            ).scalar_one_or_none()
            if region is None:
                print(f"  ! регион «{ru_name}»: не найден")
                stats["missing"] += 1
                continue
            value = _clean(uz_name)
            print(f"  [{_diff_mark(region.name_uz, value)}] {ru_name:22} → {value}")
            stats["regions"] += 1
            if apply:
                region.name_uz = value

        for slug, fields in (data.get("places") or {}).items():
            place = (
                await session.execute(select(Place).where(Place.slug == slug))
            ).scalar_one_or_none()
            if place is None:
                print(f"  ! {slug}: места нет")
                stats["missing"] += 1
                continue

            marks = []
            for field in PLACE_FIELDS:
                if field not in fields:
                    continue
                value = _clean(fields[field])
                mark = _diff_mark(getattr(place, field), value)
                marks.append(f"{field.removesuffix('_uz')}{mark}")
                if mark != "=":
                    stats["fields"] += 1
                if apply:
                    setattr(place, field, value)
            stats["places"] += 1
            print(f"  {slug:32} {fields.get('name_uz', '')[:28]:30} {' '.join(marks)}")

        for ru_name, uz_name in (data.get("tracks") or {}).items():
            tracks = (
                (await session.execute(select(PlaceTrack).where(PlaceTrack.name == ru_name)))
                .scalars()
                .all()
            )
            if not tracks:
                print(f"  ! трек «{ru_name}»: не найден")
                stats["missing"] += 1
                continue
            value = _clean(uz_name)
            print(f"  [{_diff_mark(tracks[0].name_uz, value)}] трек {ru_name:28} → {value}")
            stats["tracks"] += len(tracks)
            if apply:
                for track in tracks:
                    track.name_uz = value

        print(
            f"\nрегионов {stats['regions']}, мест {stats['places']} "
            f"(изменённых полей {stats['fields']}), треков {stats['tracks']}"
            + (f", НЕ НАЙДЕНО {stats['missing']}" if stats["missing"] else "")
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
