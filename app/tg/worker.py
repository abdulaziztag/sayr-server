"""Служба Sayr Admin: `python -m app.tg.worker` (systemd-юнит sayr-tg).

Держит один вход в Telegram, выполняет задания из `tg_jobs` и слушает
свои группы: кто вошёл по какой ссылке, сообщения, правки, удаления,
/leave. Раз в минуту отмечается в `tg_status`, раз в пять минут сверяет
входы по ссылкам — вдруг событие не дошло.

Без ключей или строки сессии не падает, а ждёт: отмечается «ждёт входа»,
и комнаты живут без групп. Строку сессии даёт `python -m app.tg.login`.
"""

import asyncio
import logging

from ..config import settings
from ..db import SessionLocal
from . import service
from .api import TelethonApi

log = logging.getLogger("sayr.tg")

JOBS_EVERY = 2
HEARTBEAT_EVERY = 60
RECONCILE_EVERY = 300


async def _idle(state: str, note: str) -> None:
    """Входа нет — жить и отмечаться, пока владелец не войдёт и не перезапустит"""
    log.warning("Sayr Admin не работает: %s", note)
    while True:
        async with SessionLocal() as session:
            await service.heartbeat(session, state, note)
            await session.commit()
        await asyncio.sleep(HEARTBEAT_EVERY)


def _register(client, api: TelethonApi) -> None:
    from telethon import events, utils
    from telethon.tl import types

    def channel_id(marked: int | None) -> int | None:
        return utils.resolve_id(marked)[0] if marked is not None else None

    @client.on(events.Raw(types.UpdateChannelParticipant))
    async def participant(update):
        # Вход по ссылке: в обновлении есть сама ссылка — связываем сразу
        if not update.new_participant or update.prev_participant or not update.invite:
            return
        link = getattr(update.invite, "link", None)
        try:
            entity = await client.get_input_entity(update.user_id)
            user_hash = getattr(entity, "access_hash", 0)
        except (ValueError, TypeError):
            user_hash = 0
        async with SessionLocal() as session:
            await service.on_join(api, session, update.channel_id, update.user_id, user_hash, link)

    @client.on(events.ChatAction)
    async def action(event):
        # Запасной путь: событие о входе без ссылки — сверяем ссылки комнаты
        if not (event.user_joined or event.user_added):
            return
        async with SessionLocal() as session:
            room = await service.room_by_chat(session, channel_id(event.chat_id))
            if room is not None:
                await service.reconcile_room(api, session, room)

    @client.on(events.NewMessage(incoming=True))
    async def message(event):
        if not event.is_group:
            return
        async with SessionLocal() as session:
            await service.on_message(
                session,
                channel_id(event.chat_id),
                event.id,
                event.sender_id,
                event.raw_text or "",
                event.media is not None,
                event.date,
            )

    @client.on(events.MessageEdited(incoming=True))
    async def edited(event):
        if not event.is_group:
            return
        async with SessionLocal() as session:
            await service.on_edit(
                session, channel_id(event.chat_id), event.id, event.raw_text or "",
                event.edit_date or event.date,
            )

    @client.on(events.MessageDeleted)
    async def deleted(event):
        if event.chat_id is None:
            return
        async with SessionLocal() as session:
            await service.on_delete(session, channel_id(event.chat_id), list(event.deleted_ids))


async def _loop(api: TelethonApi) -> None:
    tick = 0
    while True:
        try:
            async with SessionLocal() as session:
                await service.run_due(api, session)
                if tick % (HEARTBEAT_EVERY // JOBS_EVERY) == 0:
                    await service.heartbeat(session, "ok")
                    await session.commit()
                if tick % (RECONCILE_EVERY // JOBS_EVERY) == 0:
                    await service.reconcile_all(api, session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — служба должна жить дальше
            log.warning("тик службы не прошёл", exc_info=True)
        tick += 1
        await asyncio.sleep(JOBS_EVERY)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not (settings.tg_api_id and settings.tg_api_hash and settings.tg_session):
        await _idle("waiting_login", "нет ключей или строки сессии в .env")
        return

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(settings.tg_session), settings.tg_api_id, settings.tg_api_hash
    )
    await client.connect()
    if not await client.is_user_authorized():
        await _idle("session_invalid", "строка сессии не пускает — войдите заново")
        return
    me = await client.get_me()
    log.info("Sayr Admin на связи: %s (id %s)", me.first_name, me.id)

    api = TelethonApi(client)
    _register(client, api)
    # Кэш собеседников: без него события из групп приходят без ключей доступа
    await client.get_dialogs()
    await _loop(api)


if __name__ == "__main__":
    asyncio.run(main())
