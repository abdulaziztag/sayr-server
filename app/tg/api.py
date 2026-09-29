"""Что службе нужно от Telegram — и прослойка над Telethon.

Логика (`service.py`) знает только этот набор действий, поэтому проверяется
на подменном клиенте, без сети и без настоящего аккаунта. Ошибки Telethon
переводятся в свои: подождать, аккаунт ограничен, группы больше нет,
человека в группе нет.

Всё проверено по документации Telegram API и Telethon 1.45 (спека
попутчиков, «Проверено по Telegram API»): группу может создать только
пользовательский аккаунт; личная ссылка — `exportChatInvite` с
`usage_limit = 1`; кто вошёл по ссылке — `getChatInviteImporters`;
погасить ссылку — `editExportedChatInvite` с `revoked`.
"""

from datetime import datetime
from pathlib import Path
from typing import Protocol

#: Группа для вызовов: (id, access_hash)
Chat = tuple[int, int]


class TgError(Exception):
    pass


class FloodWait(TgError):
    """Telegram просит подождать — не ошибка, а пауза"""

    def __init__(self, seconds: int):
        super().__init__(f"FLOOD_WAIT {seconds}")
        self.seconds = seconds


class Restricted(TgError):
    """Аккаунт ограничен или упёрся в предел групп: группы больше не создаются"""


class Gone(TgError):
    """Группы больше нет или нас в ней нет — с группой делать нечего"""


class NotParticipant(TgError):
    """Человека в группе нет — вышел сам. С группой всё в порядке: это не
    Gone, иначе один вышедший «закрывал» бы службе всю группу"""


class TgApi(Protocol):
    async def create_group(self, title: str, about: str) -> Chat: ...
    async def set_photo(self, chat: Chat, path: Path) -> None: ...
    async def show_history(self, chat: Chat) -> None: ...
    async def send(self, chat: Chat, text: str) -> int: ...
    async def pin(self, chat: Chat, message_id: int) -> None: ...
    async def export_link(self, chat: Chat, title: str, expire: datetime) -> str: ...
    async def revoke_link(self, chat: Chat, link: str) -> None: ...
    async def link_importers(self, chat: Chat, link: str) -> list[tuple[int, int]]: ...
    async def promote(self, chat: Chat, user_id: int, user_hash: int) -> None: ...
    async def kick(self, chat: Chat, user_id: int, user_hash: int) -> None: ...
    async def leave(self, chat: Chat) -> None: ...


class TelethonApi:
    """Боевая реализация. Импорты Telethon внутри: тестам и API он не нужен"""

    def __init__(self, client):
        self.client = client

    async def _call(self, request):
        from telethon import errors

        try:
            return await self.client(request)
        except errors.FloodWaitError as e:
            raise FloodWait(e.seconds) from e
        except (errors.UserRestrictedError, errors.ChannelsTooMuchError) as e:
            raise Restricted(type(e).__name__) from e
        except (errors.ChannelPrivateError, errors.ChannelInvalidError) as e:
            raise Gone(type(e).__name__) from e
        except (errors.UserNotParticipantError, errors.ParticipantIdInvalidError) as e:
            raise NotParticipant(type(e).__name__) from e

    @staticmethod
    def _channel(chat: Chat):
        from telethon.tl.types import InputChannel

        return InputChannel(chat[0], chat[1])

    @staticmethod
    def _peer(chat: Chat):
        from telethon.tl.types import InputPeerChannel

        return InputPeerChannel(chat[0], chat[1])

    async def create_group(self, title: str, about: str) -> Chat:
        from telethon.tl.functions.channels import CreateChannelRequest

        result = await self._call(CreateChannelRequest(title=title, about=about, megagroup=True))
        channel = result.chats[0]
        return channel.id, channel.access_hash

    async def set_photo(self, chat: Chat, path: Path) -> None:
        from telethon.tl.functions.channels import EditPhotoRequest
        from telethon.tl.types import InputChatUploadedPhoto

        uploaded = await self.client.upload_file(str(path))
        await self._call(EditPhotoRequest(self._channel(chat), InputChatUploadedPhoto(file=uploaded)))

    async def show_history(self, chat: Chat) -> None:
        from telethon import errors
        from telethon.tl.functions.channels import TogglePreHistoryHiddenRequest

        try:
            await self._call(TogglePreHistoryHiddenRequest(self._channel(chat), enabled=False))
        except errors.ChatNotModifiedError:
            pass  # уже видна: повтор сборки после сбоя

    async def send(self, chat: Chat, text: str) -> int:
        from telethon import errors

        try:
            message = await self.client.send_message(self._peer(chat), text, link_preview=False)
        except errors.FloodWaitError as e:
            raise FloodWait(e.seconds) from e
        except (errors.ChannelPrivateError, errors.ChannelInvalidError) as e:
            raise Gone(type(e).__name__) from e
        return message.id

    async def pin(self, chat: Chat, message_id: int) -> None:
        from telethon import errors
        from telethon.tl.functions.messages import UpdatePinnedMessageRequest

        try:
            await self._call(
                UpdatePinnedMessageRequest(peer=self._peer(chat), id=message_id, silent=True)
            )
        except errors.ChatNotModifiedError:
            pass  # уже закреплено: повтор сборки после сбоя

    async def export_link(self, chat: Chat, title: str, expire: datetime) -> str:
        from telethon.tl.functions.messages import ExportChatInviteRequest

        result = await self._call(
            ExportChatInviteRequest(
                peer=self._peer(chat), expire_date=expire, usage_limit=1, title=title
            )
        )
        return result.link

    async def revoke_link(self, chat: Chat, link: str) -> None:
        """Погасить личную ссылку. Истёкшая или уже погашенная — не ошибка:
        войти по ней и так нельзя, а это всё, что нужно. CHAT_NOT_MODIFIED —
        тоже: так Telegram может ответить на повторное гашение"""
        from telethon import errors
        from telethon.tl.functions.messages import EditExportedChatInviteRequest

        try:
            await self._call(
                EditExportedChatInviteRequest(peer=self._peer(chat), link=link, revoked=True)
            )
        except (
            errors.InviteHashExpiredError,
            errors.InviteHashInvalidError,
            errors.InviteRevokedMissingError,
            errors.ChatNotModifiedError,
        ):
            pass

    async def link_importers(self, chat: Chat, link: str) -> list[tuple[int, int]]:
        from telethon.tl.functions.messages import GetChatInviteImportersRequest
        from telethon.tl.types import InputUserEmpty

        result = await self._call(
            GetChatInviteImportersRequest(
                peer=self._peer(chat),
                offset_date=None,
                offset_user=InputUserEmpty(),
                limit=10,
                link=link,
            )
        )
        hashes = {u.id: u.access_hash for u in result.users}
        return [(i.user_id, hashes.get(i.user_id, 0)) for i in result.importers]

    async def promote(self, chat: Chat, user_id: int, user_hash: int) -> None:
        from telethon.tl.functions.channels import EditAdminRequest
        from telethon.tl.types import ChatAdminRights, InputUser

        rights = ChatAdminRights(
            change_info=True,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            manage_call=True,
            other=True,
        )
        await self._call(
            EditAdminRequest(
                channel=self._channel(chat),
                user_id=InputUser(user_id, user_hash),
                admin_rights=rights,
                rank="Организатор",
            )
        )

    async def kick(self, chat: Chat, user_id: int, user_hash: int) -> None:
        """Выгнать, а не забанить: бан и сразу снятие — так Telegram и кикает"""
        from telethon.tl.functions.channels import EditBannedRequest
        from telethon.tl.types import ChatBannedRights, InputPeerUser

        peer = InputPeerUser(user_id, user_hash)
        await self._call(
            EditBannedRequest(
                self._channel(chat), peer, ChatBannedRights(until_date=None, view_messages=True)
            )
        )
        await self._call(
            EditBannedRequest(self._channel(chat), peer, ChatBannedRights(until_date=None))
        )

    async def leave(self, chat: Chat) -> None:
        from telethon.tl.functions.channels import LeaveChannelRequest

        await self._call(LeaveChannelRequest(self._channel(chat)))
