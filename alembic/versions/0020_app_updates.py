"""Порог версии приложения по платформам: принудительное обновление.

По строке на платформу, заводятся здесь же с версией 0.0.0 и снятым флагом:
админка их только правит, а приложения спрашивают GET /api/v1/app/update.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-05

"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "app_updates",
        sa.Column("platform", sa.String(8), primary_key=True),
        sa.Column("min_version", sa.String(16), nullable=False, server_default="0.0.0"),
        sa.Column("force", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.bulk_insert(table, [{"platform": "ios"}, {"platform": "android"}])


def downgrade() -> None:
    op.drop_table("app_updates")
