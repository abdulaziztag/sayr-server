"""Импорт треков из форума «ГОРЕЦ» в существующие места.

    uv run python -m seed.import_gorets            # показать, что будет
    uv run python -m seed.import_gorets --apply    # выполнить

Источник — выгрузка группы из Telegram Desktop: почти 500 файлов GPX, KML
и KMZ за десять лет. Отбор сделан заранее и лежит в data/gorets_map.json,
сами треки — уже очищенные и прорежённые — в data/gorets/. Так импорт
воспроизводится на сервере, куда тринадцатигигабайтную выгрузку не тащат.

Как отбирали 42 трека из 493:

- **автомобильные** отсечены по скорости внутри файлов: у трёх сотен есть
  время, и медианная скорость делит чисто — пешком до 6 км/ч, машина за 12;
- **мимо проходящие** — маршрут принадлежит месту, только если он начинается
  или кончается рядом с ним. Иначе траверс через район, задевший вершину
  краем, приписывался ей как «подъём»;
- **дубли** схлопнуты по геометрии: совпадение больше 80% — одна тропа;
- **не больше трёх на место**, однодневные вперёд — это основной сценарий;
- остальное вычищено руками: чужие вершины, заезды, обрывки и один трек,
  который уже лежал в каталоге.

Длинные маршруты (за 30 км) берём, но в наклейки места они не идут: по ним
считается окно выезда, а сорок километров за день не проходят.
"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

try:
    from fastapi_storages import StorageFile
except ImportError:  # расположение менялось между версиями пакета
    from fastapi_storages.base import StorageFile

from app.config import GPX_DIR
from app.db import SessionLocal
from app.models import Place, PlaceTrack, gpx_storage

DATA_DIR = Path(__file__).resolve().parent / "data"
MAP_FILE = DATA_DIR / "gorets_map.json"
GPX_SRC = DATA_DIR / "gorets"

# Дольше этого — не выход на день: формула окна выезда
# (закат − дорога×2 − ход×1,5 − запас) на таком уходит в минус
DAY_KM = 30.0


async def run(apply: bool) -> None:
    plan = json.loads(MAP_FILE.read_text("utf-8"))
    added = skipped = 0

    async with SessionLocal() as session:
        for slug, tracks in plan.items():
            place = (
                await session.execute(select(Place).where(Place.slug == slug))
            ).scalar_one_or_none()
            if place is None:
                print(f"  ! {slug}: места нет в базе")
                continue

            existing = {
                Path(t.gpx_file.name).name: t
                for t in (
                    await session.execute(
                        select(PlaceTrack).where(PlaceTrack.place_id == place.id)
                    )
                ).scalars()
            }
            print(f"\n{place.name}")
            for order, spec in enumerate(tracks, start=len(existing)):
                src = GPX_SRC / spec["file"]
                if not src.exists():
                    print(f"  ! нет файла {spec['file']}")
                    continue
                if spec["file"] in existing:
                    print(f"  «{spec['name'][:40]}» уже есть")
                    skipped += 1
                    continue

                gain = f"+{spec['ascent']} м" if spec["ascent"] else "набор не считаем"
                tail = "" if spec["day"] else "  (многодневный)"
                print(f"  + «{spec['name'][:40]}» {spec['km']} км, {gain}{tail}")
                added += 1
                if not apply:
                    continue
                (GPX_DIR / spec["file"]).write_bytes(src.read_bytes())
                session.add(
                    PlaceTrack(
                        place_id=place.id,
                        gpx_file=StorageFile(name=spec["file"], storage=gpx_storage),
                        name=spec["name"],
                        gpx_credit=spec["credit"],
                        distance_km=spec["km"],
                        ascent_m=spec["ascent"],
                        sort_order=order,
                    )
                )

            _fill_effort(place, tracks, apply)

        if apply:
            await session.commit()
            print(f"\nГотово: добавлено {added}, пропущено {skipped}.")
        else:
            print(f"\nПоказ без изменений: добавится {added}. Выполнить: --apply")


def _fill_effort(place: Place, tracks: list[dict], apply: bool) -> None:
    """Длина и набор для наклеек на главной — только по однодневным.

    Заполняем лишь пустое: где числа выверены руками, они точнее наших.
    Времени у «Горца» нет — в постах его не пишут; час ставит человек
    сам, либо оно приходит из tabiatsari.
    """
    day = [t for t in tracks if t["day"]]
    if not day:
        return
    longest = max(day, key=lambda t: t["km"])

    if place.distance_km is None:
        print(f"  длина — → {longest['km']} км")
        if apply:
            place.distance_km = longest["km"]

    if place.elevation_gain_m is None and longest["ascent"]:
        print(f"  набор — → {longest['ascent']} м")
        if apply:
            place.elevation_gain_m = longest["ascent"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Показ по умолчанию: скрипт правит боевой каталог
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
