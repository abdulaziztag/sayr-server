"""Статистика владельца: сырые события, дневные агрегаты, устройства.

Бэкфилла нет — просмотры начинаются с нуля с момента выкладки.
Голоса «пойду» на дашборде видны сразу: они уже лежат в trip_intents.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-15

"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("device", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_events_ts", "api_events", ["ts"])
    op.create_index("ix_api_events_kind_ts", "api_events", ["kind", "ts"])

    op.create_table(
        "daily_stats",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("active_devices", sa.Integer(), nullable=False),
        sa.Column("new_devices", sa.Integer(), nullable=False),
        sa.Column("place_opens", sa.Integer(), nullable=False),
        sa.Column("catalog_opens", sa.Integer(), nullable=False),
        sa.Column("gpx_downloads", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )

    op.create_table(
        "devices",
        sa.Column("device", sa.String(length=64), nullable=False),
        sa.Column("first_seen", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("device"),
    )
    op.create_index("ix_devices_first_seen", "devices", ["first_seen"])


def downgrade() -> None:
    op.drop_index("ix_devices_first_seen", table_name="devices")
    op.drop_table("devices")
    op.drop_table("daily_stats")
    op.drop_index("ix_api_events_kind_ts", table_name="api_events")
    op.drop_index("ix_api_events_ts", table_name="api_events")
    op.drop_table("api_events")
