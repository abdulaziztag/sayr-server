"""Досчитать универсальные свёртки за прошлые дни.

    .venv/bin/python -m app.stats_backfill --days 29

Ротация пересчитывает закрытый день, пока сырьё за него целиком в окне
хранения и число событий разошлось со свёрткой, — дни без daily_counts
внутри окна она досчитает сама. Эта команда прогоняет rollup_day
по каждому закрытому дню из последних N без сверки; она идемпотентна —
перезапись, не сложение, — поэтому лишний запуск ничего не портит.
Дни у края окна, которые чистка уже подъела, она пропускает, как
и ротация: свёртка по остатку затёрла бы полные числа меньшими, —
поэтому и --days больше срока хранения ничего не испортит.

daily_platform за прошлое останется почти пустым: заголовка с платформой
ещё не было, и устройства лягут в 'unknown'. Это правда, а не дыра.
"""

import argparse
import asyncio
from datetime import date, timedelta

from .db import SessionLocal
from .stats import _edge, rollup_day, rotate_weeks


async def run(days: int, today: date | None = None) -> list[date]:
    today = today or date.today()
    done: list[date] = []
    async with SessionLocal() as session:
        edge = await _edge(session, today)
        for back in range(days, 0, -1):
            day = today - timedelta(days=back)
            if day <= edge:
                continue
            await rollup_day(session, day)
            done.append(day)
        await session.commit()
        await rotate_weeks(session, today)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=29, help="сколько закрытых дней досчитать")
    args = parser.parse_args()
    done = asyncio.run(run(args.days))
    print(f"досчитано дней: {len(done)} ({done[0]} — {done[-1]})" if done else "нечего досчитывать")


if __name__ == "__main__":
    main()
