"""Матрица «город выезда × место»: минуты и километры дороги.

Прежде время в дороге считалось только от Ташкента и лежало в полях места.
Теперь у каждого места строка на каждый город из справочника app/cities.py;
поля мест остаются посчитанными от Ташкента — их читают старые сборки, а
новые берут как запасной вариант, когда пары нет в матрице. Строки пишет
seed/enrich_drive_times.py, по таймеру раз в месяц.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-03

"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "place_drive_times",
        sa.Column(
            "place_id",
            sa.Integer(),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("city", sa.String(length=32), primary_key=True),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("km", sa.Float(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("place_drive_times")
