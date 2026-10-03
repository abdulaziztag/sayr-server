"""Длина похода и час выезда у записи дня: план возвращается из облака.

Выход теперь убирает с телефона планы, дневник и избранное, а вход
восстанавливает план из записи дня — после повторного входа и на втором
телефоне. Месту и дню для этого мало: нужны длина похода (`days`)
и час выезда плана в день 1 (`depart_minutes`, минуты от полуночи) — без
них многодневка вернулась бы однодневкой, а полоса выезда показывала бы
расчёт от заката вместо часа из плана.

Обе колонки необязательные: у расчётных выходов плана по дням нет,
а сборки до этой правки поля не шлют — тогда клиент берёт длину
из каталога.

Спека: docs/superpowers/specs/2026-10-03-account-data-merge-design.md

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-03

"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_trip_days", sa.Column("days", sa.Integer(), nullable=True))
    op.add_column("user_trip_days", sa.Column("depart_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    # Сами записи дня остаются: пропадают только длина и час, и вход снова
    # восстанавливает план с длиной из каталога, как для старых сборок
    op.drop_column("user_trip_days", "depart_minutes")
    op.drop_column("user_trip_days", "days")
