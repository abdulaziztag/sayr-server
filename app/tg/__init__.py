"""Sayr Admin — аккаунт Telegram, который заводит группы комнат попутчиков.

Отдельная служба `sayr-tg` (`python -m app.tg.worker`) рядом с API: API
ставит задания в `tg_jobs`, служба их выполняет и слушает свои группы.
Логика — в `service.py` и проверяется на подменном клиенте; `api.py` —
тонкая прослойка над Telethon. Спека
docs/superpowers/specs/2026-09-27-companions-design.md, часть 2.
"""
