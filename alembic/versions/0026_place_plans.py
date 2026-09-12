"""Планы по дням: место → план → день → станция.

Расчётная нить однодневки считает световой день; для Аделунги нужен план
клуба — выезд в два ночи, погранпост, лагерь, подъём в полчетвёртого.
Такой план пишет человек, здесь он лежит структурой: план (вариант
выхода), дни с треком, станции семи видов со временем или длительностью
(спека docs/superpowers/specs/2026-09-13-multiday-plan-design.md).

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-13

"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_KINDS = ("depart", "point", "hike", "road", "summit", "night", "home")


def upgrade() -> None:
    op.create_table(
        "place_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "place_id", sa.Integer(),
            sa.ForeignKey("places.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("title_uz", sa.String(length=120), nullable=True),
        sa.Column("is_draft", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_place_plans_place_id", "place_plans", ["place_id"])

    op.create_table(
        "plan_days",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "plan_id", sa.Integer(),
            sa.ForeignKey("place_plans.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("title_uz", sa.String(length=120), nullable=True),
        sa.Column(
            "track_id", sa.Integer(),
            sa.ForeignKey("place_tracks.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("reversed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("plan_id", "n", name="uq_plan_day"),
    )
    op.create_index("ix_plan_days_plan_id", "plan_days", ["plan_id"])

    # Тип создаёт сам create_table: отдельный create() давал «type already
    # exists», потому что колонка Enum создаёт его ещё раз
    kind = sa.Enum(*_KINDS, name="plan_step_kind")
    op.create_table(
        "plan_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "day_id", sa.Integer(),
            sa.ForeignKey("plan_days.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", kind, nullable=False, server_default="point"),
        sa.Column("at", sa.Time(), nullable=True),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("title_uz", sa.String(length=200), nullable=True),
        sa.Column("sub", sa.String(length=200), nullable=True),
        sa.Column("sub_uz", sa.String(length=200), nullable=True),
    )
    op.create_index("ix_plan_steps_day_id", "plan_steps", ["day_id"])


def downgrade() -> None:
    op.drop_index("ix_plan_steps_day_id", table_name="plan_steps")
    op.drop_table("plan_steps")
    sa.Enum(name="plan_step_kind").drop(op.get_bind(), checkfirst=True)
    op.drop_index("ix_plan_days_plan_id", table_name="plan_days")
    op.drop_table("plan_days")
    op.drop_index("ix_place_plans_place_id", table_name="place_plans")
    op.drop_table("place_plans")
