"""Заводит ЧЕРНОВИКАМИ новые места из треков телеграм-канала владельца.

    uv run python -m seed.import_channel_places            # показать
    uv run python -m seed.import_channel_places --apply    # записать
    ... --files /путь/к/выгрузке --credit "«ГОРЕЦ»"

План — seed/data/channel_new_places.json: отбор из 61 кластера сделан руками
(35 мест), там же список пропущенных файлов с причинами. Имена — из имён
файлов и тегов, живьём их никто не проверял: править в админке, публикация
за человеком.

Что выводится из трека само: точка места (самая дальняя точка маршрута
от старта — для вершины это вершина, для пещеры вход), высота (если в записи
есть), регион (по ближайшему месту каталога), длина/время/набор. Категория
и название — из плана.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import joinedload

try:
    from fastapi_storages import StorageFile
except ImportError:  # расположение менялось между версиями пакета
    from fastapi_storages.base import StorageFile

from app.config import GPX_DIR
from app.db import SessionLocal
from app.models import Difficulty, Place, PlaceCategory, PlaceTrack, gpx_storage
from app.services.gpx import (
    clean,
    haversine_m,
    outbound_only,
    track_stats,
)

from .estimate_duration import estimate
from .import_channel import (
    DEFAULT_FILES_DIR,
    MAX_DAY_HOURS,
    MAX_TRACK_KM,
    Candidate,
    _load_gpx,
    resolve_export_path,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
PLAN_FILE = DATA_DIR / "channel_new_places.json"


def _goal(data: bytes) -> tuple[float, float, int | None]:
    """Самая дальняя от старта точка трека и её высота.

    Для выхода на вершину это вершина, для пещеры — вход: ровно та точка,
    которой место и является. Координата места из середины прямоугольника
    была бы точкой в воздухе над долиной.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(data)
    ns = root.tag[: root.tag.index("}") + 1] if "}" in root.tag else ""
    pts: list[tuple[float, float, float | None]] = []
    for pt in root.iter(f"{ns}trkpt"):
        ele = pt.find(f"{ns}ele")
        pts.append((
            float(pt.get("lat")),
            float(pt.get("lon")),
            float(ele.text) if ele is not None and ele.text else None,
        ))
    start = pts[0]
    far = max(pts, key=lambda p: haversine_m(start[0], start[1], p[0], p[1]))
    return round(far[0], 6), round(far[1], 6), round(far[2]) if far[2] else None


def _prepare(files_dir: Path, slug: str, spec_file: str) -> Candidate | None:
    """Файл выгрузки → кандидат с очищенным содержимым, как в import_channel."""
    path = resolve_export_path(files_dir, spec_file)
    if path is None:
        print(f"  ! файла нет в выгрузке: {spec_file}")
        return None
    raw = _load_gpx(path)
    if raw is None:
        print(f"  ! не разобрался: {spec_file}")
        return None
    round_trip = track_stats(raw).distance_km
    cut = outbound_only(raw)
    data = clean(cut if cut is not None else raw)
    stats = track_stats(data)
    if stats.distance_km > MAX_TRACK_KM:
        print(f"  ~ пропущен «{spec_file}» — {stats.distance_km} км, многодневный")
        return None
    # Обрывки — не маршруты: 200-метровый «траверс» стал бы у места
    # и главной наклейкой длины, и единственной правдой о выходе
    if stats.distance_km < 0.5:
        print(f"  ~ пропущен «{spec_file}» — {stats.distance_km} км, обрывок")
        return None
    digest = hashlib.md5(spec_file.encode()).hexdigest()[:6]
    title = Path(spec_file).stem.replace("_", " ").strip()
    return Candidate(
        src=spec_file,
        gpx_name=f"{slug}-channel-{digest}.gpx",
        title=title,
        by_place=False,
        data=data,
        stats=stats,
        round_trip_km=round_trip,
        cut=cut is not None,
    )


async def run(apply: bool, credit: str, files_dir: Path) -> None:
    plan = json.loads(PLAN_FILE.read_text("utf-8"))

    created = existed = 0
    async with SessionLocal() as session:
        catalog = (
            (await session.execute(select(Place).options(joinedload(Place.region))))
            .unique()
            .scalars()
            .all()
        )
        seen: set[str] = set()
        for spec in plan["places"]:
            slug = spec["slug"]
            if slug in seen:
                print(f"  ! {spec['name']}: слаг {slug} уже занят в плане, пропущено")
                continue
            seen.add(slug)
            if any(p.slug == slug for p in catalog):
                existed += 1
                continue

            candidates = [
                c for f in spec["files"] if (c := _prepare(files_dir, slug, f))
            ]
            if not candidates:
                print(f"! {spec['name']}: ни один файл не годен — место не заводим")
                continue

            # Точка места — по самому длинному кандидату: он вернее всего
            # доходит до самой цели, короткие бывают лишь подходами
            main = max(candidates, key=lambda c: c.stats.distance_km)
            lat, lng, ele = _goal(main.data)

            nearest = min(catalog, key=lambda p: haversine_m(lat, lng, p.lat, p.lng))
            shortest = min(candidates, key=lambda c: c.round_trip_km)
            hours = estimate(shortest.round_trip_km, shortest.stats.ascent_m)

            created += 1
            print(
                f"+ {spec['name'][:30]:30} {spec['category']:9} "
                f"({lat}, {lng}) {ele or '—':>5} м  регион: {nearest.region.name[:14]:14} "
                f"треков: {len(candidates)}  {shortest.round_trip_km} км"
            )
            if not apply:
                continue

            place = Place(
                slug=slug,
                name=spec["name"],
                category=PlaceCategory(spec["category"]),
                # Средняя, пока человек не оценил сам: у нас только геометрия
                difficulty=Difficulty.medium,
                region_id=nearest.region_id,
                lat=lat,
                lng=lng,
                elevation_m=ele,
                distance_km=shortest.round_trip_km,
                duration_hours=hours if hours <= MAX_DAY_HOURS else None,
                elevation_gain_m=shortest.stats.ascent_m or None,
                # Черновик: показывается после названия-описания и публикации
                is_published=False,
            )
            session.add(place)
            await session.flush()

            for order, cand in enumerate(sorted(candidates, key=lambda c: c.stats.distance_km)):
                (GPX_DIR / cand.gpx_name).write_bytes(cand.data)
                session.add(
                    PlaceTrack(
                        place_id=place.id,
                        gpx_file=StorageFile(name=cand.gpx_name, storage=gpx_storage),
                        name=cand.title,
                        gpx_credit=credit,
                        distance_km=cand.stats.distance_km,
                        ascent_m=cand.stats.ascent_m,
                        start_lat=cand.stats.start_lat,
                        start_lng=cand.stats.start_lng,
                        sort_order=order,
                    )
                )

        if apply:
            await session.commit()
            print(f"\nГотово: заведено {created}, уже было {existed}.")
        else:
            print(f"\nПоказ без изменений: заведётся {created}, уже есть {existed}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    parser.add_argument("--credit", default="", help="подпись автора треков")
    parser.add_argument("--files", type=Path, default=DEFAULT_FILES_DIR)
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.credit, args.files))


if __name__ == "__main__":
    main()
