"""places.overnight — способ ночёвки на многодневках

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05

"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

overnight_type = sa.Enum("tent", "yurt", name="overnight_type")


def upgrade() -> None:
    overnight_type.create(op.get_bind(), checkfirst=True)
    op.add_column("places", sa.Column("overnight", overnight_type, nullable=True))


def downgrade() -> None:
    op.drop_column("places", "overnight")
    overnight_type.drop(op.get_bind(), checkfirst=True)
