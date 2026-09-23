"""Аккаунты: люди, сессии устройств, заявки на код.

Человек — это номер телефона; анкета (имя, фамилия, пол, год рождения,
ник в телеграме, фото) лежит в той же таблице и до заполнения пустая.
Сессия — вход на одном устройстве, в базе отпечаток токена, а не токен.
Заявка на код кода не содержит: его придумывает и проверяет шлюз
(спека docs/superpowers/specs/2026-09-23-account-login-design.md).

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-23

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

GENDER = sa.Enum("male", "female", "unspecified", name="gender")
# В create_table тип нельзя заводить второй раз: его уже создал GENDER.create
# выше, и CREATE TYPE упал бы на «тип уже существует»
GENDER_COL = postgresql.ENUM("male", "female", "unspecified", name="gender", create_type=False)


def upgrade() -> None:
    GENDER.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_name", sa.String(length=60), server_default="", nullable=False),
        sa.Column("last_name", sa.String(length=60), server_default="", nullable=False),
        sa.Column("gender", GENDER_COL, nullable=True),
        sa.Column("birth_year", sa.Integer(), nullable=True),
        sa.Column("telegram_username", sa.String(length=40), nullable=True),
        sa.Column("avatar", sa.String(length=255), nullable=True),
        sa.Column("profile_filled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=True),
        sa.Column("platform", sa.String(length=8), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index(
        "ix_user_sessions_token_hash", "user_sessions", ["token_hash"], unique=True
    )

    op.create_table(
        "login_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("gateway_request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="sent", nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
    )
    op.create_index("ix_login_requests_phone", "login_requests", ["phone"])
    op.create_index("ix_login_requests_created_at", "login_requests", ["created_at"])


def downgrade() -> None:
    op.drop_table("login_requests")
    op.drop_table("user_sessions")
    op.drop_table("users")
    GENDER.drop(op.get_bind(), checkfirst=True)
