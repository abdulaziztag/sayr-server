"""Прикинуть ходовое время там, где его нет: `uv run python -m seed.estimate_duration`.

Треки с форума «ГОРЕЦ» приходят без оценки времени — люди выкладывают файл,
а «шли шесть часов» не пишут. Считаем по правилу Нейсмита: час на четыре
километра пути плюс час на каждые шестьсот метров набора. Оно старое
и грубое, зато проверенное горами и честнее пустой наклейки.

Трогаем только пустое: где время выверено руками или пришло из tabiatsari,
оно точнее расчёта. Число округляем до получаса — точность здесь мнимая,
и «7,3 часа» обещали бы больше, чем формула знает.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place

KM_PER_HOUR = 4.0
ASCENT_M_PER_HOUR = 600.0


def estimate(distance_km: float, ascent_m: int | None) -> float:
    hours = distance_km / KM_PER_HOUR + (ascent_m or 0) / ASCENT_M_PER_HOUR
    return round(hours * 2) / 2


async def run(apply: bool) -> None:
    async with SessionLocal() as session:
        places = (
            await session.execute(
                select(Place).where(
                    Place.duration_hours.is_(None), Place.distance_km.is_not(None)
                )
            )
        ).scalars().all()

        for place in places:
            hours = estimate(place.distance_km, place.elevation_gain_m)
            gain = f"+{place.elevation_gain_m} м" if place.elevation_gain_m else "без набора"
            print(f"  {place.name[:26]:26} {place.distance_km:>5} км, {gain:>12} → {hours} ч")
            if apply:
                place.duration_hours = hours

        if apply:
            await session.commit()
            print(f"\nГотово: проставлено {len(places)}.")
        else:
            print(f"\nПоказ без изменений: проставится {len(places)}. Выполнить: --apply")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
