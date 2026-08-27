"""Как прошёл поход: подтверждение выхода и темп.

До сих пор отметка «Пойду» была только намерением — подтверждения,
что человек действительно сходил, в базе не существовало. Вечером дня
выхода приложение теперь спрашивает «были?», и ответ приземляется сюда.

Два поля дописываются в существующую строку `trip_intents`: у неё уже
есть уникальный ключ (place_id, day, device_id), поэтому «одна поправка
на один поход» получается само собой, без новой логики.

Оба nullable и без server_default — по той же причине, что и узбекские
колонки в 0012: «не ответил» обязано отличаться от «ответил».

Счётчик вынесен в отдельную таблицу, потому что `trip_intents` чистится
через срок хранения (30 дней). Личная строка досиживает свой срок
и уходит, а накопленная картина по местам нужна навсегда — ровно ради
неё всё и затевалось: у 83 мест ходовое время посчитано формулой,
и поправить его сейчас неоткуда.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-27

"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trip_intents", sa.Column("went", sa.Boolean(), nullable=True))
    op.add_column("trip_intents", sa.Column("pace", sa.String(length=8), nullable=True))

    op.create_table(
        "place_pace_stats",
        sa.Column(
            "place_id",
            sa.Integer(),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("faster", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("slower", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("place_pace_stats")
    op.drop_column("trip_intents", "pace")
    op.drop_column("trip_intents", "went")
