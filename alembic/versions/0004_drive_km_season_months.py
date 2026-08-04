"""places: drive_km и сезон диапазоном месяцев

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-05

"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("places", sa.Column("drive_km", sa.Float(), nullable=True))
    op.add_column("places", sa.Column("season_from", sa.Integer(), nullable=True))
    op.add_column("places", sa.Column("season_to", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("places", "season_to")
    op.drop_column("places", "season_from")
    op.drop_column("places", "drive_km")
