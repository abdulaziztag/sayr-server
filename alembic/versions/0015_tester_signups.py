"""Заявки на закрытый тест Android — почта с лендинга.

Google Play пускает в закрытый тест только адреса из списка в консоли,
поэтому «скачать для Android» пока выглядит так: человек оставляет
почту на лендинге, владелец руками добавляет её в список тестировщиков
и присылает ссылку. Таблица — входящая очередь этого процесса.

`invited` — галочка «уже добавил и написал», ставится в админке; без неё
при десятке заявок уже не вспомнить, кому ссылка ушла, а кому нет.

Почта уникальна: повторная отправка формы не плодит строк и не выдаёт
наружу, есть адрес в базе или нет.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-28

"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tester_signups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False, unique=True),
        sa.Column("lang", sa.String(length=2), nullable=False, server_default="ru"),
        sa.Column(
            "invited", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("tester_signups")
