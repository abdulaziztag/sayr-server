import os
import tempfile

os.environ.setdefault(
    "SAYR_DATABASE_URL", "postgresql+psycopg://sayr:sayr@localhost:5432/sayr_test"
)
os.environ.setdefault("SAYR_MEDIA_DIR", tempfile.mkdtemp(prefix="sayr-media-"))

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.db import engine
from app.main import app
from app.models import Base, Difficulty, Place, PlaceCategory, Region

# Ташкент ~ (41.31, 69.28). Гулькам-тест ~85 км, «ближнее озеро» ~60 км, Заамин ~200 км.
FIXTURES = [
    dict(
        slug="test-waterfall",
        name="Тестовый водопад",
        category=PlaceCategory.waterfall,
        difficulty=Difficulty.easy,
        lat=41.62,
        lng=70.10,
        elevation_m=1500,
        best_seasons=["spring", "summer"],
        kid_friendly=True,
        short_desc="Красивый водопад для теста",
    ),
    dict(
        slug="test-peak",
        name="Тестовый пик",
        category=PlaceCategory.peak,
        difficulty=Difficulty.hard,
        lat=41.50,
        lng=70.03,
        elevation_m=3300,
        best_seasons=["summer", "autumn"],
        kid_friendly=False,
        short_desc="Суровый пик для теста",
    ),
    dict(
        slug="test-lake",
        name="Тестовое озеро",
        category=PlaceCategory.lake,
        difficulty=Difficulty.easy,
        lat=41.60,
        lng=69.95,
        elevation_m=900,
        best_seasons=["summer"],
        kid_friendly=True,
        short_desc="Озеро недалеко от города",
    ),
    dict(
        slug="test-far-plateau",
        name="Дальнее плато",
        category=PlaceCategory.plateau,
        difficulty=Difficulty.medium,
        lat=39.96,
        lng=68.40,
        elevation_m=2000,
        best_seasons=["spring", "autumn"],
        kid_friendly=False,
        short_desc="Плато за пределами радиуса",
    ),
]


@pytest.fixture(scope="session", autouse=True)
async def prepare_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    from app.db import SessionLocal

    async with SessionLocal() as session:
        region = Region(name="Тестовый регион", sort_order=0)
        session.add(region)
        await session.flush()
        for data in FIXTURES:
            session.add(Place(region_id=region.id, **data))
        await session.commit()

    yield

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
