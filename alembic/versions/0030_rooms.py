"""Попутчики: комнаты, участники, жалобы, блокировки, задания Sayr Admin,
личные пуши.

Статусы — строками, а не типами Postgres: у них будут появляться значения,
и менять строку проще, чем тип (урок 0028 с CREATE TYPE gender).
Спека docs/superpowers/specs/2026-09-27-companions-design.md.

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-27

"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def _now() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("companions_banned_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "rooms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(8), nullable=False),
        sa.Column("invite", sa.String(16), nullable=False),
        sa.Column(
            "place_id",
            sa.Integer(),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organizer_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("days", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_open", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        _now(),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("tg_state", sa.String(16), server_default="none", nullable=False),
        sa.Column("tg_left_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_rooms_code", "rooms", ["code"], unique=True)
    op.create_index("ix_rooms_invite", "rooms", ["invite"], unique=True)
    op.create_index("ix_rooms_place_id", "rooms", ["place_id"])
    op.create_index("ix_rooms_organizer_id", "rooms", ["organizer_id"])
    op.create_index("ix_rooms_day", "rooms", ["day"])
    op.create_index("ix_rooms_status", "rooms", ["status"])

    op.create_table(
        "room_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("transport", sa.String(16), nullable=True),
        sa.Column("seats", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(140), server_default="", nullable=False),
        _now(),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("tg_link", sa.String(64), nullable=True),
        sa.Column("tg_link_used", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.UniqueConstraint("room_id", "user_id", name="uq_room_member"),
    )
    op.create_index("ix_room_members_room_id", "room_members", ["room_id"])
    op.create_index("ix_room_members_user_id", "room_members", ["user_id"])
    op.create_index("ix_room_members_status", "room_members", ["status"])

    op.create_table(
        "room_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reporter_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "target_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("text", sa.String(500), server_default="", nullable=False),
        _now(),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(200), nullable=True),
    )
    op.create_index("ix_room_reports_target_user_id", "room_reports", ["target_user_id"])
    op.create_index("ix_room_reports_created_at", "room_reports", ["created_at"])

    op.create_table(
        "user_blocks",
        sa.Column(
            "blocker_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "blocked_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        _now(),
    )
    op.create_index("ix_user_blocks_blocked_id", "user_blocks", ["blocked_id"])

    op.create_table(
        "tg_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column(
            "member_id",
            sa.Integer(),
            sa.ForeignKey("room_members.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        _now(),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tg_jobs_room_id", "tg_jobs", ["room_id"])
    op.create_index("ix_tg_jobs_status", "tg_jobs", ["status"])
    op.create_index("ix_tg_jobs_run_after", "tg_jobs", ["run_after"])

    op.create_table(
        "push_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("room_code", sa.String(8), nullable=True),
        _now(),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_push_outbox_user_id", "push_outbox", ["user_id"])
    op.create_index("ix_push_outbox_sent_at", "push_outbox", ["sent_at"])


def downgrade() -> None:
    op.drop_table("push_outbox")
    op.drop_table("tg_jobs")
    op.drop_table("user_blocks")
    op.drop_table("room_reports")
    op.drop_table("room_members")
    op.drop_table("rooms")
    op.drop_column("users", "companions_banned_at")
