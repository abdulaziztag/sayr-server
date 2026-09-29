"""Сверка по часам сервера, индексы под лимиты входа и потолок событий.

У избранного, истории выходов и города выезда появляется время записи
на сервере. Сверка отбирала новое после `since` по времени правки на
телефоне, а `since` — время сервера: правка, сделанная без связи или на
телефоне с отстающими часами, до второго телефона не доезжала никогда.
Старым строкам время записи берём из времени правки — ничего не
переотправится разом.

Заодно срезаем время правки из будущего: телефон с убежавшими часами
выигрывал бы у всех правок до той даты. Дальше это делает сама сверка.

Лимиты входа теперь считаются по заявкам в базе — им нужны индексы по
номеру, адресу и устройству вместе со временем: счёт читает только своё
окно. Индекс только по номеру им заменён. Суточному потолку платных кодов —
частичный индекс без неотправленных заявок и заявок проверяющего.
Суточный потолок событий с телефона считает события устройства,
у api_events.device индекса не было.

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-29

"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_SYNCED = ("user_favorites", "user_trip_days", "user_settings")
#: Условие частичного индекса — слово в слово как _PAID в app/api/auth.py
_PAID = "status <> 'unsent' AND channel <> 'test'"


def upgrade() -> None:
    for table in _SYNCED:
        op.add_column(
            table,
            sa.Column(
                "server_updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.execute(f"UPDATE {table} SET updated_at = now() WHERE updated_at > now()")
        if table != "user_settings":
            op.execute(f"UPDATE {table} SET deleted_at = now() WHERE deleted_at > now()")
        op.execute(f"UPDATE {table} SET server_updated_at = updated_at")

    op.create_index(
        "ix_login_requests_phone_created_at", "login_requests", ["phone", "created_at"]
    )
    op.drop_index("ix_login_requests_phone", "login_requests")
    op.create_index(
        "ix_login_requests_ip_created_at", "login_requests", ["ip", "created_at"]
    )
    op.create_index(
        "ix_login_requests_device_id_created_at", "login_requests", ["device_id", "created_at"]
    )
    op.create_index(
        "ix_login_requests_codes_created_at",
        "login_requests",
        ["created_at"],
        postgresql_where=sa.text(_PAID),
    )
    op.create_index("ix_api_events_device_ts", "api_events", ["device", "ts"])


def downgrade() -> None:
    op.drop_index("ix_api_events_device_ts", "api_events")
    op.drop_index("ix_login_requests_codes_created_at", "login_requests")
    op.drop_index("ix_login_requests_device_id_created_at", "login_requests")
    op.drop_index("ix_login_requests_ip_created_at", "login_requests")
    op.create_index("ix_login_requests_phone", "login_requests", ["phone"])
    op.drop_index("ix_login_requests_phone_created_at", "login_requests")
    for table in reversed(_SYNCED):
        op.drop_column(table, "server_updated_at")
