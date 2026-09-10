"""Ответы игры «когда сюда идти»: дуги по кругу года.

Сезон места собирается толпой на сайте и применяется к месту только после
одобрения проверяющим, поэтому ответы живут отдельной таблицей, а не
в `places`. Пара «место + номер устройства» уникальна: второй ответ
с того же телефона перезаписывает первый.

Месяцы nullable — это «не знаю»: в подсчёт такая строка не идёт, но место
этому устройству больше не выпадает.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-11

"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "season_votes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "place_id",
            sa.Integer(),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_month", sa.Integer(), nullable=True),
        sa.Column("to_month", sa.Integer(), nullable=True),
        sa.Column("voter", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("place_id", "voter", name="uq_season_vote"),
    )
    # Колода спрашивает «на что это устройство уже отвечало» и «сколько
    # ответов у места» — оба запроса идут по этим колонкам
    op.create_index("ix_season_votes_place_id", "season_votes", ["place_id"])
    op.create_index("ix_season_votes_voter", "season_votes", ["voter"])


def downgrade() -> None:
    op.drop_index("ix_season_votes_voter", table_name="season_votes")
    op.drop_index("ix_season_votes_place_id", table_name="season_votes")
    op.drop_table("season_votes")
