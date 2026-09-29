"""Первый вход Sayr Admin: `uv run --with qrcode python -m app.tg.login`
на сервере, по ssh.

Два способа. QR-код: скрипт рисует его в терминале, телефон с открытым
Sayr Admin сканирует его в «Настройки → Устройства → Подключить устройство»;
код никуда не отправляется — надёжнее всего, новым аккаунтам Telegram
коды для входа через API доставляет не всегда. Кодом по номеру: скрипт
пишет словами, куда Telegram его отправил (в приложение, SMS, почту — или
туда, куда он не дойдёт: Firebase умеют только официальные приложения);
пустой ввод вместо кода — выслать другим способом. Потом, если стоит,
пароль двухэтапной проверки.

Ключи и строку сессии дописывает в `.env`, сессию — закомментированной:
убрать `#` и `systemctl restart sayr-tg` — отдельный осознанный шаг.
Строка — это вход в аккаунт: никуда больше её не копировать.
"""

import asyncio
import re
from getpass import getpass

from ..config import SERVER_DIR, settings

ENV = SERVER_DIR / ".env"


def _where(sent) -> str:
    """Куда ушёл код и что можно сделать дальше — словами"""
    from telethon.tl import types

    t = sent.type
    a = types.auth
    if isinstance(t, a.SentCodeTypeApp):
        text = ("в приложение Telegram: чат «Telegram» с синей галочкой там, где "
                "открыт аккаунт Sayr Admin (в приложении с несколькими аккаунтами "
                "— переключиться на него)")
    elif isinstance(t, a.SentCodeTypeSms):
        text = "SMS на этот номер"
    elif isinstance(t, (a.SentCodeTypeSmsWord, a.SentCodeTypeSmsPhrase)):
        text = "SMS со словом или фразой — ввести их целиком"
    elif isinstance(t, a.SentCodeTypeCall):
        text = "звонком: робот продиктует код"
    elif isinstance(t, a.SentCodeTypeMissedCall):
        text = (f"сброшенным звонком с номера {t.prefix}…: код — последние "
                f"{t.length} цифр этого номера")
    elif isinstance(t, a.SentCodeTypeFlashCall):
        text = "сброшенным звонком: код — номер, с которого позвонили"
    elif isinstance(t, a.SentCodeTypeEmailCode):
        text = f"на почту {t.email_pattern}"
    elif isinstance(t, a.SentCodeTypeFragmentSms):
        text = f"через Fragment: {t.url}"
    elif isinstance(t, a.SentCodeTypeFirebaseSms):
        text = ("SMS через Firebase — так умеют только официальные приложения, "
                "сюда код не дойдёт")
    else:
        text = f"способом {type(t).__name__}"
    lines = [f"\nКод отправлен {text}."]
    if sent.next_type:
        later = f" (не раньше чем через {sent.timeout} с)" if sent.timeout else ""
        lines.append(f"Пустой ввод — выслать иначе: {_how(sent.next_type)}{later}.")
    return "\n".join(lines)


def _how(code_type) -> str:
    return {
        "CodeTypeSms": "SMS",
        "CodeTypeCall": "звонком",
        "CodeTypeFlashCall": "сброшенным звонком",
        "CodeTypeMissedCall": "сброшенным звонком",
        "CodeTypeFragmentSms": "через Fragment",
    }.get(type(code_type).__name__, type(code_type).__name__)


async def _settle(client, phone: str, sent):
    """Ответ на запрос кода, из которого уже можно входить.

    Новым аккаунтам Telegram иногда вместо кода велит сначала привязать почту
    для входа (SetUpEmailRequired) — привязываем тут же, код придёт на неё;
    иногда хочет денег за SMS (PaymentRequired) — тут скрипт бессилен"""
    from telethon import errors
    from telethon.tl import functions, types

    if isinstance(sent, types.auth.SentCodePaymentRequired):
        raise SystemExit(
            "Telegram просит оплатить SMS со входом — из скрипта это не сделать. "
            "Попробуйте позже или с другим api_id.")
    if not isinstance(sent.type, types.auth.SentCodeTypeSetUpEmailRequired):
        return sent
    print("\nTelegram не шлёт код, пока к аккаунту не привязана почта для входа.")
    purpose = types.EmailVerifyPurposeLoginSetup(phone, sent.phone_code_hash)
    while True:
        email = input("Почта для входа в Sayr Admin: ").strip()
        try:
            sent_email = await client(functions.account.SendVerifyEmailCodeRequest(purpose, email))
            break
        except errors.RPCError as e:
            print(f"Telegram не принял почту: {e}")
    print(f"Код отправлен на {sent_email.email_pattern}.")
    while True:
        code = input("Код из письма: ").strip()
        try:
            verified = await client(functions.account.VerifyEmailRequest(
                purpose, types.EmailVerificationCode(code)))
        except errors.RPCError as e:
            print(f"Не подошёл: {e}")
            continue
        return await _settle(client, phone, verified.sent_code)


async def _send(client, phone: str):
    from telethon.tl import functions, types

    sent = await client(functions.auth.SendCodeRequest(
        phone, client.api_id, client.api_hash, types.CodeSettings()))
    return await _settle(client, phone, sent)


async def _resend(client, phone: str, sent):
    from telethon import errors
    from telethon.tl import functions

    try:
        again = await client(functions.auth.ResendCodeRequest(phone, sent.phone_code_hash))
    except errors.SendCodeUnavailableError:
        print("Других способов у Telegram нет — ждите код там, куда он ушёл.")
        return sent
    except errors.PhoneCodeExpiredError:
        return await _send(client, phone)
    except errors.FloodWaitError as e:
        print(f"Telegram просит подождать {e.seconds} с.")
        return sent
    return await _settle(client, phone, again)


async def _sign_in(client, phone: str, sent, code: str) -> None:
    """Код с почты проверяется отдельным полем запроса, остальные — обычным"""
    from telethon.tl import functions, types

    if isinstance(sent.type, types.auth.SentCodeTypeEmailCode):
        result = await client(functions.auth.SignInRequest(
            phone, sent.phone_code_hash,
            email_verification=types.EmailVerificationCode(code)))
        await client._on_login(result.user)
    else:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)


async def _password(client) -> None:
    from telethon import errors

    while True:
        try:
            await client.sign_in(password=getpass("Пароль двухэтапной проверки: "))
            return
        except errors.PasswordHashInvalidError:
            print("Пароль не тот.")


def _draw(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        print(f"\nНет пакета qrcode — запустите через uv run --with qrcode. Ссылка для QR: {url}")
        return
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    # Терминал тёмный: светлыми рисуем светлые клетки, тёмные — фон
    qr.print_ascii(invert=True)


async def _qr_login(client) -> None:
    """Токен в QR живёт ~30 с — по истечении рисуем новый, пока не отсканируют"""
    import datetime

    from telethon import errors

    login = await client.qr_login()
    while True:
        print("\nТелефон с Sayr Admin: Настройки → Устройства → Подключить устройство — "
              "и навести камеру на код.")
        _draw(login.url)
        left = (login.expires - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        try:
            await login.wait(timeout=max(left, 5))
            return
        except asyncio.TimeoutError:
            await login.recreate()
        except errors.SessionPasswordNeededError:
            await _password(client)
            return


async def _code_login(client, phone: str) -> None:
    from telethon import errors

    try:
        sent = await _send(client, phone)
    except errors.PhoneNumberInvalidError:
        raise SystemExit("Telegram не знает такого номера — проверьте, что он с +998.")
    except errors.PhoneNumberBannedError:
        raise SystemExit("Номер заблокирован в Telegram.")
    except errors.FloodWaitError as e:
        raise SystemExit(f"Слишком много попыток — Telegram просит подождать {e.seconds} с.")
    print(_where(sent))
    while True:
        code = input("Код (пусто — выслать иначе): ").strip()
        if not code:
            sent = await _resend(client, phone, sent)
            print(_where(sent))
            continue
        try:
            await _sign_in(client, phone, sent, code)
            return
        except errors.SessionPasswordNeededError:
            await _password(client)
            return
        except (errors.PhoneCodeInvalidError, errors.CodeInvalidError):
            print("Код не тот — ещё раз.")
        except errors.PhoneCodeExpiredError:
            print("Код истёк — высылаю новый.")
            sent = await _send(client, phone)
            print(_where(sent))
        except errors.RPCError as e:
            print(f"Telegram ответил: {e}")


def _remember(api_id: int, api_hash: str, session: str) -> None:
    """Дописывает в .env то, чего там ещё нет; уже записанное не трогает"""
    text = ENV.read_text() if ENV.exists() else ""
    add = []
    if not re.search(r"^\s*SAYR_TG_API_ID\s*=", text, re.M):
        add.append(f"SAYR_TG_API_ID={api_id}")
    if not re.search(r"^\s*SAYR_TG_API_HASH\s*=", text, re.M):
        add.append(f"SAYR_TG_API_HASH={api_hash}")
    if re.search(r"^[\s#]*SAYR_TG_SESSION\s*=", text, re.M):
        print(f"В {ENV} уже есть строка SAYR_TG_SESSION — не трогаю. Новая сессия:\n")
        print(f"SAYR_TG_SESSION={session}\n")
    else:
        add += ["# Sayr Admin: убрать # и systemctl restart sayr-tg — бот начнёт заводить группы",
                f"# SAYR_TG_SESSION={session}"]
    if not add:
        return
    with ENV.open("a") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write("\n".join(add) + "\n")
    print(f"Записал в {ENV}: " + ", ".join(line.split("=")[0].lstrip("# ") for line in add if "=" in line))


async def main() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = settings.tg_api_id or int(input("api_id с my.telegram.org: "))
    api_hash = settings.tg_api_hash or input("api_hash с my.telegram.org: ").strip()
    way = input("Вход: 1 — QR-кодом с телефона, 2 — кодом по номеру [1]: ").strip() or "1"
    phone = "" if way == "1" else re.sub(r"[^\d+]", "", input("Номер Sayr Admin (+998…): "))
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        if way == "1":
            await _qr_login(client)
        else:
            await _code_login(client, phone)
        me = await client.get_me()
        print(f"\nВошли как {me.first_name} (id {me.id}).")
        _remember(api_id, api_hash, client.session.save())
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
