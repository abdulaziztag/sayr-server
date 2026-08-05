"""trip_intents — кто и на какую дату собирается в место

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-05

"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trip_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "place_id",
            sa.Integer(),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        # Аккаунтов нет: голос привязан к устройству
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("place_id", "day", "device_id", name="uq_intent_place_day_device"),
    )
    op.create_index("ix_trip_intents_place_day", "trip_intents", ["place_id", "day"])


def downgrade() -> None:
    op.drop_table("trip_intents")
