"""Первый вход Sayr Admin: `python -m app.tg.login` на сервере, по ssh.

Спросит номер сим-карты Sayr Admin, код из Telegram и пароль двухэтапной
проверки, а напечатает строку сессии — её в `.env` как SAYR_TG_SESSION
и `systemctl restart sayr-tg`. Строка — это вход в аккаунт: никуда больше
её не копировать.
"""

import asyncio
from getpass import getpass

from ..config import settings


async def main() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = settings.tg_api_id or int(input("api_id с my.telegram.org: "))
    api_hash = settings.tg_api_hash or input("api_hash с my.telegram.org: ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(
        phone=lambda: input("Номер Sayr Admin (+998…): ").strip(),
        code_callback=lambda: input("Код из Telegram: ").strip(),
        password=lambda: getpass("Пароль двухэтапной проверки: "),
    )
    me = await client.get_me()
    print(f"\nВошли как {me.first_name} (id {me.id}).")
    print("Строка сессии — в .env одной строкой:\n")
    print(f"SAYR_TG_SESSION={client.session.save()}\n")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
