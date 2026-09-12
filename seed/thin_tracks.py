"""Проредить уже загруженные треки длиннее MAX_POINTS точек.

    uv run python -m seed.thin_tracks           # показать, что и на сколько похудеет
    uv run python -m seed.thin_tracks --apply   # переписать файлы и статистику

Прореживание при загрузке появилось 13 сентября 2026; всё, что залили
в админку раньше сырым с часов, лежит мегабайтами и качается клиентом
целиком. Скрипт разовый: берёт треки длиннее порога, переписывает файл
через `gpx.clean` и пересчитывает длину, набор и точку старта по новому
файлу. Соседей (`link_neighbors`) не трогает — геометрия та же.

Перед --apply на проде — копия папки треков:
    tar czf /var/backups/sayr/gpx-pre-thin.tgz -C /root/Projects/sayr-server media/gpx
"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select

from app.config import GPX_DIR
from app.db import SessionLocal
from app.models import PlaceTrack
from app.services.gpx import MAX_POINTS, clean, track_coords, track_stats


async def run(apply: bool) -> list[tuple[str, int, int]]:
    """Что похудело: имя трека, точек было, точек стало."""
    report: list[tuple[str, int, int]] = []
    async with SessionLocal() as session:
        tracks = (await session.execute(select(PlaceTrack))).scalars().all()
        for track in tracks:
            if not track.gpx_file:
                continue
            path = GPX_DIR / Path(str(track.gpx_file.name)).name
            if not path.exists():
                continue
            data = path.read_bytes()
            try:
                before = len(track_coords(data))
            except Exception:
                continue
            if before <= MAX_POINTS:
                continue
            small = clean(data)
            report.append((track.name, before, len(track_coords(small))))
            if apply:
                path.write_bytes(small)
                stats = track_stats(small)
                track.distance_km = stats.distance_km
                track.ascent_m = stats.ascent_m
                track.start_lat = stats.start_lat
                track.start_lng = stats.start_lng
        if apply:
            await session.commit()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="переписать файлы и статистику")
    args = parser.parse_args()
    report = asyncio.run(run(args.apply))
    for name, before, after in report:
        print(f"  {name}: {before} → {after} точек")
    verb = "переписано" if args.apply else "похудеет"
    print(f"{len(report)} трек(ов) {verb}" + ("" if args.apply else "; добавьте --apply"))


if __name__ == "__main__":
    main()
