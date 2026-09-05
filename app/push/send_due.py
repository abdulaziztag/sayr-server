"""Тик планировщика пушей: `python -m app.push.send_due`.

Запускается системным таймером sayr-push.timer раз в минуту. Собирает
отправителей из настроек — платформа без ключей просто не попадает
в словарь — и рассылает созревшие объявления.
"""

import asyncio

from ..config import settings
from ..db import SessionLocal
from .sender import Transport, run_once


def build_transports() -> tuple[dict[str, Transport], list]:
    transports: dict[str, Transport] = {}
    closers = []
    if settings.apns_key_path and settings.apns_key_id:
        from .apns import ApnsSender

        apns = ApnsSender(
            settings.apns_key_path, settings.apns_key_id, settings.apns_team_id,
            settings.apns_topic, settings.apns_sandbox,
        )
        transports["ios"] = apns.send
        closers.append(apns.aclose)
    if settings.fcm_service_account_path:
        from .fcm import FcmSender

        fcm = FcmSender(settings.fcm_service_account_path)
        transports["android"] = fcm.send
        closers.append(fcm.aclose)
    return transports, closers


async def _main() -> None:
    transports, closers = build_transports()
    try:
        async with SessionLocal() as session:
            done = await run_once(session, transports)
    finally:
        for close in closers:
            await close()
    if not done:
        return
    for a in done:
        print(f"{a.send_at:%d.%m %H:%M} «{a.title}»: {a.status}, дошло {a.sent_count}, не дошло {a.failed_count}"
              + (f" — {a.last_error}" if a.last_error else ""))


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
