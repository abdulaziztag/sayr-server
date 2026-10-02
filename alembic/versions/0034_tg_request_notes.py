"""Заметки Sayr Admin о заявках: номер сообщения в группе у заявки.

Кто-то попросился в комнату — Sayr Admin пишет в группу «Мадина просится
в поход» со ссылкой на комнату. При одобрении заметку правит, при отказе,
отзыве, отмене похода и когда поход прошёл — удаляет. Номер сообщения
лежит в строке заявки, а не в задании: разбирают заявку и через месяц,
когда выполненные задания уже убраны уборкой (rooms.housekeeping), а
одобрение и отказ трогают как раз эту строку.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-02

"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "room_members", sa.Column("tg_request_message_id", sa.BigInteger(), nullable=True)
    )


def downgrade() -> None:
    # Висящие заметки останутся в группах как есть: без номера служба их
    # уже не найдёт, а приложению и API колонка не нужна
    op.drop_column("room_members", "tg_request_message_id")
