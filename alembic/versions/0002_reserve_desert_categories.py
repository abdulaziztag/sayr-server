"""Категории reserve (нацпарк) и desert (пустыня/барханы)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-04

"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE place_category ADD VALUE IF NOT EXISTS 'reserve'")
    op.execute("ALTER TYPE place_category ADD VALUE IF NOT EXISTS 'desert'")


def downgrade() -> None:
    # Удаление значения из PG enum штатно невозможно; значения безвредны.
    pass
