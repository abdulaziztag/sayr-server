"""Досчитать универсальные свёртки за прошлые дни.

    .venv/bin/python -m app.stats_backfill --days 30

Ротация сворачивает день один раз — когда для него появляется строка
daily_stats. У дней, закрытых до появления daily_counts, такая строка
уже есть, и ротация их не тронет. Эта команда прогоняет rollup_day
по каждому закрытому дню из последних N; она идемпотентна — перезапись,
не сложение, — поэтому лишний запуск ничего не портит.

daily_platform за прошлое останется почти пустым: заголовка с платформой
ещё не было, и устройства лягут в 'unknown'. Это правда, а не дыра.
"""

import argparse
import asyncio
from datetime import date, timedelta

from .db import SessionLocal
from .stats import rollup_day, rotate_weeks


async def run(days: int, today: date | None = None) -> list[date]:
    today = today or date.today()
    done: list[date] = []
    async with SessionLocal() as session:
        for back in range(days, 0, -1):
            day = today - timedelta(days=back)
            await rollup_day(session, day)
            done.append(day)
        await session.commit()
        await rotate_weeks(session, today)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=30, help="сколько закрытых дней досчитать")
    args = parser.parse_args()
    done = asyncio.run(run(args.days))
    print(f"досчитано дней: {len(done)} ({done[0]} — {done[-1]})" if done else "нечего досчитывать")


if __name__ == "__main__":
    main()
