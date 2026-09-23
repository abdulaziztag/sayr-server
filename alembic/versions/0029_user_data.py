"""Своё в аккаунте: избранное, история выходов, город выезда, планы.

Три таблицы с отметкой времени правки и отметкой удаления: без второй
снятое на одном телефоне сердечко вернулось бы со второго. Планы получают
человека — у гостя по-прежнему только устройство
(спека docs/superpowers/specs/2026-09-23-account-login-design.md).

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-23

"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_favorites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "place_id",
            sa.Integer(),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "place_id", name="uq_user_favorite"),
    )
    op.create_index("ix_user_favorites_user_id", "user_favorites", ["user_id"])
    op.create_index("ix_user_favorites_updated_at", "user_favorites", ["updated_at"])

    op.create_table(
        "user_trip_days",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "place_id",
            sa.Integer(),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("pace", sa.String(length=8), nullable=True),
        sa.Column("distance_km", sa.Float(), nullable=True),
        sa.Column("elevation_gain_m", sa.Integer(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "place_id", "day", name="uq_user_trip_day"),
    )
    op.create_index("ix_user_trip_days_user_id", "user_trip_days", ["user_id"])
    op.create_index("ix_user_trip_days_updated_at", "user_trip_days", ["updated_at"])

    op.create_table(
        "user_settings",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("departure_city", sa.String(length=32), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.add_column(
        "trip_intents",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_trip_intents_user_id", "trip_intents", ["user_id"])
    op.create_index(
        "ux_trip_intents_user_day",
        "trip_intents",
        ["place_id", "day", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_trip_intents_user_day", table_name="trip_intents")
    op.drop_index("ix_trip_intents_user_id", table_name="trip_intents")
    op.drop_column("trip_intents", "user_id")
    op.drop_table("user_settings")
    op.drop_table("user_trip_days")
    op.drop_table("user_favorites")
