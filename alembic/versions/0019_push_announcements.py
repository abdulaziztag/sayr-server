"""Пуши из админки: токены установок и объявления по расписанию.

`push_tokens` — по строке на установку, протухшие гасятся, а не удаляются.
`announcements` — заголовок, текст, время по Ташкенту без зоны, статус
и счётчики доставки. Поля аудитории заведены сразу, чтобы не делать
миграцию под обещанные позже фильтры (спека 2026-09-05-push-announcements-design.md).

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-05

"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_tokens",
        sa.Column("token", sa.String(512), primary_key=True),
        sa.Column("platform", sa.String(8), nullable=False),
        sa.Column("device", sa.String(64), nullable=True),
        sa.Column("lang", sa.String(2), nullable=False, server_default="ru"),
        sa.Column("city", sa.String(32), nullable=True),
        sa.Column("app_version", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.String(64), nullable=True),
    )
    op.create_index("ix_push_tokens_platform", "push_tokens", ["platform"])
    op.create_index("ix_push_tokens_device", "push_tokens", ["device"])

    op.create_table(
        "announcements",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("place_slug", sa.String(64), nullable=True),
        sa.Column("send_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="scheduled"),
        sa.Column("audience_lang", sa.String(2), nullable=True),
        sa.Column("audience_city", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
    )
    op.create_index("ix_announcements_send_at", "announcements", ["send_at"])
    op.create_index("ix_announcements_status", "announcements", ["status"])


def downgrade() -> None:
    op.drop_index("ix_announcements_status", table_name="announcements")
    op.drop_index("ix_announcements_send_at", table_name="announcements")
    op.drop_table("announcements")
    op.drop_index("ix_push_tokens_device", table_name="push_tokens")
    op.drop_index("ix_push_tokens_platform", table_name="push_tokens")
    op.drop_table("push_tokens")
