from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place
from seed.seed import seed

# Место из places.json, где длина и время стоят null: именно такие
# затирались импортированными значениями при каждом прогоне сида
SLUG = "pulatkhan-plateau"


async def test_seed_keeps_imported_stats():
    """Пустое поле в places.json не затирает заполненное в базе.

    Длину, время и набор проставляют импорты из tabiatsari и «Горца»,
    а в сид-файле у большинства мест там null. Прогон сида ради правки
    соседнего поля однажды снёс всё импортированное разом.
    """
    await seed()

    async with SessionLocal() as session:
        place = (
            await session.execute(select(Place).where(Place.slug == SLUG))
        ).scalar_one()
        assert place.distance_km is None, "в сид-файле у этого места длины нет"
        place.distance_km = 12.5
        place.duration_hours = 6.0
        await session.commit()

    await seed()

    async with SessionLocal() as session:
        place = (
            await session.execute(select(Place).where(Place.slug == SLUG))
        ).scalar_one()
        assert place.distance_km == 12.5
        assert place.duration_hours == 6.0
