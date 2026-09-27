"""Sayr Admin: переписка групп, состояние службы, ключи доступа Telegram.

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-27

"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("tg_access_hash", sa.BigInteger(), nullable=True))
    op.create_index("ix_rooms_tg_chat_id", "rooms", ["tg_chat_id"])
    op.add_column("room_members", sa.Column("tg_user_hash", sa.BigInteger(), nullable=True))

    op.create_table(
        "tg_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("has_media", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.UniqueConstraint("room_id", "tg_message_id", name="uq_tg_message"),
    )
    op.create_index("ix_tg_messages_room_id", "tg_messages", ["room_id"])
    op.create_index("ix_tg_messages_user_id", "tg_messages", ["user_id"])
    op.create_index("ix_tg_messages_sent_at", "tg_messages", ["sent_at"])

    op.create_table(
        "tg_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("groups", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("tg_status")
    op.drop_table("tg_messages")
    op.drop_column("room_members", "tg_user_hash")
    op.drop_index("ix_rooms_tg_chat_id", "rooms")
    op.drop_column("rooms", "tg_access_hash")
