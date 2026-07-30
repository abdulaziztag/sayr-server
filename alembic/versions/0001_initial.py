"""Каталог: regions, places, place_photos

Revision ID: 0001
Revises:
Create Date: 2026-07-30

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

place_category = sa.Enum(
    "waterfall",
    "peak",
    "gorge",
    "cave",
    "lake",
    "canyon",
    "spring",
    "plateau",
    "petroglyphs",
    "other",
    name="place_category",
)
difficulty = sa.Enum("easy", "medium", "hard", name="difficulty")


def upgrade() -> None:
    op.create_table(
        "regions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "places",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", place_category, nullable=False),
        sa.Column("region_id", sa.Integer(), sa.ForeignKey("regions.id"), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("elevation_m", sa.Integer(), nullable=True),
        sa.Column("difficulty", difficulty, nullable=False),
        sa.Column("distance_km", sa.Float(), nullable=True),
        sa.Column("duration_hours", sa.Float(), nullable=True),
        sa.Column("elevation_gain_m", sa.Integer(), nullable=True),
        sa.Column(
            "best_seasons",
            postgresql.ARRAY(sa.String(16)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("kid_friendly", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("short_desc", sa.Text(), nullable=False, server_default=""),
        sa.Column("description_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("how_to_get_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("gpx_file", sa.String(), nullable=True),
        sa.Column("gpx_credit", sa.String(300), nullable=True),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_places_slug", "places", ["slug"], unique=True)
    op.create_index("ix_places_category", "places", ["category"])
    op.create_index("ix_places_difficulty", "places", ["difficulty"])
    op.create_index("ix_places_region_id", "places", ["region_id"])
    op.create_index("ix_places_is_published", "places", ["is_published"])

    op.create_table(
        "place_photos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "place_id",
            sa.Integer(),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file", sa.String(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credit", sa.String(300), nullable=False, server_default=""),
    )
    op.create_index("ix_place_photos_place_id", "place_photos", ["place_id"])


def downgrade() -> None:
    op.drop_table("place_photos")
    op.drop_table("places")
    op.drop_table("regions")
    bind = op.get_bind()
    place_category.drop(bind, checkfirst=True)
    difficulty.drop(bind, checkfirst=True)
