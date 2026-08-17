"""Связи «рядом»: места, мимо которых проходит трек другого места.

Таблица наполняется расчётом по геометрии треков — при сохранении трека
в админке и скриптом `seed.link_neighbors` для разового пересчёта.
Бэкфилла в миграции нет: файлы лежат на диске, а не в базе.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-18

"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "place_neighbors",
        sa.Column("place_id", sa.Integer(), nullable=False),
        sa.Column("neighbor_id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("distance_m", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["neighbor_id"], ["places.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["track_id"], ["place_tracks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("place_id", "neighbor_id", "track_id"),
    )
    # По треку удаляем при перезаливке файла; каскад от удаления трека
    # тоже ходит этим индексом
    op.create_index("ix_place_neighbors_track_id", "place_neighbors", ["track_id"])


def downgrade() -> None:
    op.drop_index("ix_place_neighbors_track_id", table_name="place_neighbors")
    op.drop_table("place_neighbors")
