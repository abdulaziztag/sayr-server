"""Вход через Telegram и Apple: способы входа у аккаунта, отзыв Apple.

У человека появляются аккаунт Telegram (`telegram_id`) и идентификатор Apple
(`apple_sub`), оба уникальные, и зашифрованный refresh-токен Apple — он нужен
только для отзыва доступа при удалении аккаунта. Номер становится
необязательным: у вошедших через Apple его нет, Telegram отдаёт его только
с согласия. Уникальный индекс номера остаётся как был — Postgres не считает
пустые значения совпадающими, так что уникальность держится среди непустых.

Отзывы Apple, которые не прошли сразу, ждут повтора в `apple_revokes`.

Спека: docs/superpowers/specs/2026-09-30-telegram-apple-login-design.md

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-30

"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("telegram_id", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("apple_sub", sa.String(length=128), nullable=True))
    op.add_column("users", sa.Column("apple_refresh", sa.Text(), nullable=True))
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)
    op.create_index("ix_users_apple_sub", "users", ["apple_sub"], unique=True)
    op.alter_column("users", "phone", existing_type=sa.String(length=20), nullable=True)

    op.create_table(
        "apple_revokes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "run_after",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_apple_revokes_run_after", "apple_revokes", ["run_after"])


def downgrade() -> None:
    # Аккаунт без номера прежняя схема хранить не умеет. Молча стереть
    # людей или выдумать им номера — хуже, чем остановиться: пусть решает
    # человек, глядя на список
    bind = op.get_bind()
    orphans = bind.execute(sa.text("SELECT count(*) FROM users WHERE phone IS NULL")).scalar()
    if orphans:
        raise RuntimeError(
            f"{orphans} аккаунтов без номера (вошли через Telegram или Apple): "
            "откат 0033 их потерял бы — сначала решите, что с ними делать"
        )
    op.drop_index("ix_apple_revokes_run_after", "apple_revokes")
    op.drop_table("apple_revokes")
    op.alter_column("users", "phone", existing_type=sa.String(length=20), nullable=False)
    op.drop_index("ix_users_apple_sub", "users")
    op.drop_index("ix_users_telegram_id", "users")
    op.drop_column("users", "apple_refresh")
    op.drop_column("users", "apple_sub")
    op.drop_column("users", "telegram_id")
