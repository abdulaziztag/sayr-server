"""Тропёжка, опасность и ограничения — в ответах игры и у мест.

После разговора с Данилой Алябьевым (11 сентября 2026) выяснилось, что
одного сезона мало. Зимой место «условно доступно» — и вопрос в том,
сколько сил уйдёт на снег. Опасность — не то же, что сложность: лёгкая
тропа бывает камнеопасной. А попасть мешает не только погода: погранзона,
заповедник, сезон охоты, временное закрытие.

Игрок отвечает на это необязательно, в той же игре. К месту значения
попадают только через одобрение проверяющим — как и сезон. Баллы от 1
до 10, ограничения — кодами из app/seasons.py (LIMITS).

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-11

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def _facts(table: str, load: str) -> None:
    op.add_column(table, sa.Column(load, sa.Integer(), nullable=True))
    op.add_column(table, sa.Column("danger", sa.Integer(), nullable=True))
    op.add_column(
        table,
        sa.Column(
            "limits",
            postgresql.ARRAY(sa.String(length=16)),
            nullable=False,
            server_default="{}",
        ),
    )


def upgrade() -> None:
    # В ответе балл за снег называется по вопросу, у места — по сути
    _facts("season_votes", "snow_load")
    op.add_column("season_votes", sa.Column("note", sa.Text(), nullable=True))
    _facts("places", "winter_load")


def downgrade() -> None:
    for column in ("winter_load", "danger", "limits"):
        op.drop_column("places", column)
    for column in ("note", "snow_load", "danger", "limits"):
        op.drop_column("season_votes", column)
