"""Матрица «город выезда → областной хаб»: минуты и километры дороги.

Далёкую локацию житель другой области берёт не напрямую, а через областной
центр рядом с ней: доехать до хаба накануне, а сам день считать как у его
жителя (спека 2026-09-03-day-window-design.md). Чтобы показать в нити
«накануне доехать до Ташкента · 4:30», нужно время между городами, а
матрица place_drive_times знает только «город → место».

Пар мало: 28 городов на 13 хабов минус совпадения. Пишет их тот же
seed/enrich_drive_times.py тем же прогоном.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-03

"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "city_drive_times",
        sa.Column("origin", sa.String(length=32), primary_key=True),
        sa.Column("hub", sa.String(length=32), primary_key=True),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("km", sa.Float(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("city_drive_times")
