"""Аналитика: события с телефона, заголовок устройства, свёртки, когорты.

Сырьё остаётся в api_events: вид расширяется до 32 знаков, появляется
случайный номер события с частичным уникальным индексом — повтор пачки
после потерянного подтверждения гасится на вставке. Устройству —
платформа, версия, язык и система из заголовка X-Sayr-App. Три новые
таблицы итогов: универсальная дневная свёртка по виду и ключу, активные
по платформам, удержание по неделям
(спека docs/superpowers/specs/2026-09-19-analytics-design.md).

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-19

"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "api_events", "kind",
        existing_type=sa.String(length=16), type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.add_column("api_events", sa.Column("client_id", sa.String(length=36), nullable=True))
    op.create_index(
        "ux_api_events_client_id", "api_events", ["client_id"], unique=True,
        postgresql_where=sa.text("client_id IS NOT NULL"),
    )

    op.add_column("devices", sa.Column("platform", sa.String(length=8), nullable=True))
    op.add_column("devices", sa.Column("app_version", sa.String(length=16), nullable=True))
    op.add_column("devices", sa.Column("lang", sa.String(length=2), nullable=True))
    op.add_column("devices", sa.Column("os_major", sa.String(length=8), nullable=True))
    op.add_column("devices", sa.Column("last_seen", sa.Date(), nullable=True))

    op.create_table(
        "daily_counts",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("kind", sa.String(length=32), primary_key=True),
        sa.Column("key", sa.String(length=160), primary_key=True, server_default=""),
        sa.Column("events", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("devices", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "daily_platform",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("platform", sa.String(length=8), primary_key=True),
        sa.Column("active", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "cohort_retention",
        sa.Column("cohort_week", sa.Date(), primary_key=True),
        sa.Column("week_index", sa.Integer(), primary_key=True),
        sa.Column("devices", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("cohort_retention")
    op.drop_table("daily_platform")
    op.drop_table("daily_counts")
    for column in ("last_seen", "os_major", "lang", "app_version", "platform"):
        op.drop_column("devices", column)
    op.drop_index("ux_api_events_client_id", table_name="api_events")
    op.drop_column("api_events", "client_id")
    op.alter_column(
        "api_events", "kind",
        existing_type=sa.String(length=32), type_=sa.String(length=16),
        existing_nullable=False,
    )
