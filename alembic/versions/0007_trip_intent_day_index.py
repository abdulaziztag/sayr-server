"""ix_trip_intents_day — индекс, который модель объявляла, а схема не имела

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-06

"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_trip_intents_day", "trip_intents", ["day"])


def downgrade() -> None:
    op.drop_index("ix_trip_intents_day", table_name="trip_intents")
