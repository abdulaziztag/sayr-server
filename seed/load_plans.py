"""Планы по дням из seed/data/plans.json — в базу.

    uv run python -m seed.load_plans                      # все места из файла
    uv run python -m seed.load_plans --only adelunga-peak

Владелец присылает план текстом, разработчик кладёт его в файл и запускает
загрузчик; на проде команду запускает владелец после выкатки. Форма
в админке — запасной путь (спека 2026-09-13-multiday-plan-design.md).

Структура файла — карта «слаг → {tracks?, plans}». Треки дня указаны
именем трека внутри места, а не номером: номера на проде и локально
разные. `tracks` необязательны и грузятся тем же `_ensure_tracks`,
что и обычный сид, — файлы из seed/data/gpx.

Идемпотентно по паре «место, название плана»: повторный запуск
переписывает у плана перевод, черновик, порядок, дни и станции,
а не плодит копии. Незнакомое место — предупреждение, не падение;
трек по имени не нашёлся — день без трека и предупреждение.
"""

import argparse
import asyncio
import json
from datetime import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models import Place, PlacePlan, PlaceTrack, PlanDay, PlanStep, PlanStepKind
from seed.seed import _ensure_tracks

DATA = Path(__file__).resolve().parent / "data" / "plans.json"


def _time(value: str | None) -> time | None:
    """«2:00» → 02:00; пусто — пусто (черновик без часов)."""
    if not value:
        return None
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _step(order: int, spec: dict) -> PlanStep:
    return PlanStep(
        sort_order=order,
        kind=PlanStepKind(spec.get("kind", "point")),
        at=_time(spec.get("time")),
        minutes=spec.get("minutes"),
        title=spec["title"],
        title_uz=spec.get("title_uz"),
        sub=spec.get("sub"),
        sub_uz=spec.get("sub_uz"),
    )


async def load(data: dict, session, only: str | None = None) -> list[str]:
    """Загрузить планы; вернуть строки отчёта — их печатает main и читают тесты."""
    log: list[str] = []
    for slug, item in data.items():
        if only and slug != only:
            continue
        place = (
            await session.execute(
                select(Place).options(selectinload(Place.tracks)).where(Place.slug == slug)
            )
        ).scalar_one_or_none()
        if place is None:
            log.append(f"! {slug}: места нет, пропущено")
            continue
        if item.get("tracks"):
            await _ensure_tracks(session, place, {"tracks": item["tracks"]})
            await session.flush()
        tracks = {
            t.name: t.id
            for t in (
                await session.execute(select(PlaceTrack).where(PlaceTrack.place_id == place.id))
            ).scalars()
        }
        for order, spec in enumerate(item.get("plans", [])):
            plan = (
                await session.execute(
                    select(PlacePlan)
                    .options(selectinload(PlacePlan.days))
                    .where(PlacePlan.place_id == place.id, PlacePlan.title == spec["title"])
                )
            ).scalar_one_or_none()
            if plan is None:
                plan = PlacePlan(place_id=place.id, title=spec["title"])
                session.add(plan)
            plan.title_uz = spec.get("title_uz")
            plan.is_draft = bool(spec.get("is_draft", False))
            plan.sort_order = spec.get("sort_order", order)
            # Дни и станции — заново целиком: план правится как одно целое,
            # и сверять построчно «что изменилось» здесь незачем
            plan.days = []
            await session.flush()
            for day_spec in spec.get("days", []):
                track_id = None
                if day_spec.get("track"):
                    track_id = tracks.get(day_spec["track"])
                    if track_id is None:
                        log.append(
                            f"! {slug}: у дня {day_spec['n']} нет трека «{day_spec['track']}»"
                        )
                day = PlanDay(
                    n=day_spec["n"],
                    title=day_spec["title"],
                    title_uz=day_spec.get("title_uz"),
                    track_id=track_id,
                    reversed=bool(day_spec.get("reversed", False)),
                )
                day.steps = [
                    _step((i + 1) * 10, s) for i, s in enumerate(day_spec.get("steps", []))
                ]
                plan.days.append(day)
            log.append(f"  {slug}: «{spec['title']}» — {len(plan.days)} дн.")
    await session.commit()
    return log


async def run(path: Path, only: str | None) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    async with SessionLocal() as session:
        return await load(data, session, only)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--file", type=Path, default=DATA)
    parser.add_argument("--only", help="один слаг из файла")
    args = parser.parse_args()
    for line in asyncio.run(run(args.file, args.only)):
        print(line)


if __name__ == "__main__":
    main()
