"""Заявки о неточностях в карточках мест — форма /report.

Каталог собран руками, и часть цифр в нём приблизительная. Правит их
дешевле всего тот, кто только что сходил: он пишет через форму, владелец
разбирает очередь в админке и связывается по оставленному контакту.

Место nullable и с ON DELETE SET NULL: заявка про место, которого нет
в каталоге, — обычное дело (человек вписывает его в place_note),
а удаление места не должно уносить с собой текст обращения.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-08

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "place_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "place_id",
            sa.Integer(),
            sa.ForeignKey("places.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("place_note", sa.String(length=200), nullable=True),
        sa.Column(
            "topics",
            postgresql.ARRAY(sa.String(length=24)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("contact", sa.String(length=120), nullable=True),
        sa.Column("lang", sa.String(length=2), nullable=False, server_default="ru"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="web"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Разбор очереди идёт двумя способами: «что нового» и «что за неделю».
    # Оба упираются в эти две колонки
    op.create_index("ix_place_reports_status", "place_reports", ["status"])
    op.create_index("ix_place_reports_created_at", "place_reports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_place_reports_created_at", table_name="place_reports")
    op.drop_index("ix_place_reports_status", table_name="place_reports")
    op.drop_table("place_reports")
