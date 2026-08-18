"""Точка старта маршрута: откуда идут пешком.

Координаты самого места — это цель: вершина, водопад, озеро. В автонавигатор
нужна другая точка — начало тропы, где оставляют машину. Берём её из первой
точки записи трека.

Колонки допускают NULL: у трека может не быть ни одной точки (битый файл),
а у места может не быть трека вовсе. Существующие треки засыпает
`seed.backfill_track_starts`.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-18

"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("place_tracks", sa.Column("start_lat", sa.Float(), nullable=True))
    op.add_column("place_tracks", sa.Column("start_lng", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("place_tracks", "start_lng")
    op.drop_column("place_tracks", "start_lat")
