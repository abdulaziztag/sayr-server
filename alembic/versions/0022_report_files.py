"""Файлы к заявкам и отметка владельца «проверено».

Два изменения одной работы — разбор очереди обращений.

Файл к заявке нужен потому, что словами несошедшийся поворот описывают
минуту и всё равно неточно, а снимком — за один тап. Таблица отдельная:
кадров прикладывают несколько, и у каждого своё имя, размер и тип.
ON DELETE CASCADE — файл без заявки не значит ничего, в отличие от
заявки без места.

`verified` — отметка владельца «так и есть, чиню». Отдельно от статуса,
потому что это разные вопросы: статус говорит, где заявка в работе,
флаг — верю ли я ей вообще. Просмотр очереди и правка карточек — две
разные посадки за стол, и вторая идёт по отмеченному на первой.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-09

"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "place_reports",
        sa.Column(
            "verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_table(
        "place_report_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "report_id",
            sa.Integer(),
            sa.ForeignKey("place_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "original_name", sa.String(length=200), nullable=False, server_default=""
        ),
        sa.Column(
            "content_type", sa.String(length=60), nullable=False, server_default=""
        ),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Файлы всегда читаются пачкой одной заявки — других запросов к таблице нет
    op.create_index(
        "ix_place_report_files_report_id", "place_report_files", ["report_id"]
    )


def downgrade() -> None:
    # Сами файлы на диске (media/reports) откат не трогает: строки уйдут,
    # а байты останутся. Это намеренно — откат схемы обычно означает,
    # что что-то пошло не так, и терять при нём чужие снимки не надо
    op.drop_index("ix_place_report_files_report_id", table_name="place_report_files")
    op.drop_table("place_report_files")
    op.drop_column("place_reports", "verified")
