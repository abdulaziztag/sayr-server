"""Треки местa переезжают в свою таблицу: маршрутов может быть несколько.

Существующий places.gpx_file становится единственной строкой place_tracks
(Большому Чимгану — сразу настоящее имя маршрута), статистика считается
из файла прямо здесь: сид и админка делают то же самое при сохранении,
но до них перенесённые строки не доживут нетронутыми.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-13

"""
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Имя маршрута для перенесённых строк. Известным трекам — настоящие имена
_KNOWN_NAMES = {"bolshoy-chimgan": "Западный гребень"}
_DEFAULT_NAME = "Маршрут"


def upgrade() -> None:
    op.create_table(
        "place_tracks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "place_id",
            sa.Integer(),
            sa.ForeignKey("places.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("gpx_file", sa.String(), nullable=False),
        sa.Column("gpx_credit", sa.String(300), nullable=True),
        sa.Column("distance_km", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ascent_m", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_place_tracks_place_id", "place_tracks", ["place_id"])

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, slug, gpx_file, gpx_credit FROM places WHERE gpx_file IS NOT NULL")
    ).fetchall()
    for place_id, slug, gpx_file, gpx_credit in rows:
        distance_km, ascent_m = _stats_for(gpx_file)
        conn.execute(
            sa.text(
                "INSERT INTO place_tracks"
                " (place_id, name, gpx_file, gpx_credit, distance_km, ascent_m, sort_order)"
                " VALUES (:p, :n, :f, :c, :d, :a, 0)"
            ),
            {
                "p": place_id,
                "n": _KNOWN_NAMES.get(slug, _DEFAULT_NAME),
                "f": gpx_file,
                "c": gpx_credit,
                "d": distance_km,
                "a": ascent_m,
            },
        )

    op.drop_column("places", "gpx_file")
    op.drop_column("places", "gpx_credit")


def downgrade() -> None:
    op.add_column("places", sa.Column("gpx_file", sa.String(), nullable=True))
    op.add_column("places", sa.Column("gpx_credit", sa.String(300), nullable=True))
    conn = op.get_bind()
    # Обратно переезжает только основной трек — больше одному месту не положено
    conn.execute(
        sa.text(
            "UPDATE places SET gpx_file = t.gpx_file, gpx_credit = t.gpx_credit"
            " FROM (SELECT DISTINCT ON (place_id) place_id, gpx_file, gpx_credit"
            "       FROM place_tracks ORDER BY place_id, sort_order, id) AS t"
            " WHERE places.id = t.place_id"
        )
    )
    op.drop_index("ix_place_tracks_place_id", table_name="place_tracks")
    op.drop_table("place_tracks")


def _stats_for(gpx_file: str) -> tuple[float, int]:
    """Длина и набор из файла; файла нет или он битый — нули, миграция не падает."""
    try:
        from app.config import GPX_DIR
        from app.services.gpx import track_stats

        stats = track_stats((GPX_DIR / Path(gpx_file).name).read_bytes())
        return stats.distance_km, stats.ascent_m
    except Exception:
        return 0.0, 0
