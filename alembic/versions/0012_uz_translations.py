"""Узбекские тексты каталога: параллельные колонки *_uz.

Интерфейс обоих приложений уже говорит по-узбекски, а содержимое каталога
приходит с сервера и остаётся русским. Кладём перевод рядом с оригиналом:
на 126 местах и двух зафиксированных в клиентах языках отдельная таблица
переводов окупается хуже, чем стоит — она отняла бы у админки готовую
форму, а у поиска простой ILIKE.

Все колонки допускают NULL, и это не мелочь. Русские тексты объявлены
NOT NULL с server_default '' (0001_initial), из-за чего «пусто» и «не
заполнено» неразличимы. Для перевода различие несущее: на нём стоит
фолбэк на русский и подсчёт готовности. Поэтому здесь — только NULL,
без server_default.

Region.name_uz намеренно без UNIQUE, в отличие от Region.name: пользы
от него нет, а при заливке заглушек он бы мешал.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-25

"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("regions", sa.Column("name_uz", sa.String(length=120), nullable=True))
    op.add_column("places", sa.Column("name_uz", sa.String(length=200), nullable=True))
    op.add_column("places", sa.Column("short_desc_uz", sa.Text(), nullable=True))
    op.add_column("places", sa.Column("description_md_uz", sa.Text(), nullable=True))
    op.add_column("places", sa.Column("how_to_get_md_uz", sa.Text(), nullable=True))
    op.add_column("place_tracks", sa.Column("name_uz", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("place_tracks", "name_uz")
    op.drop_column("places", "how_to_get_md_uz")
    op.drop_column("places", "description_md_uz")
    op.drop_column("places", "short_desc_uz")
    op.drop_column("places", "name_uz")
    op.drop_column("regions", "name_uz")
