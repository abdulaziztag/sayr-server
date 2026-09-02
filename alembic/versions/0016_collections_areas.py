"""Коллекции «Проекта 21» у мест и область у регионов.

`places.collections` — коды коллекций клуба (`cascade`, `horizon`, `mirage`,
`underground`), пусто по умолчанию; наполняет seed/apply_collections.py.
`regions.area` / `area_uz` — область: шесть районов Ташкентской области
после дробления Бостанлыка стояли в фильтре наравне с целыми областями,
а группировать их клиент может, только зная область каждого региона.
Nullable — миграция проходит без данных, области льёт seed/apply_regions.py.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-02

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "places",
        sa.Column(
            "collections",
            postgresql.ARRAY(sa.String(length=16)),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column("regions", sa.Column("area", sa.String(length=120), nullable=True))
    op.add_column("regions", sa.Column("area_uz", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("regions", "area_uz")
    op.drop_column("regions", "area")
    op.drop_column("places", "collections")
