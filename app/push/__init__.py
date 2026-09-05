"""Пуши по расписанию: отправители APNs и FCM, планировщик созревших объявлений."""

from dataclasses import dataclass


@dataclass
class SendResult:
    """Ответ платформы по одному токену."""

    ok: bool
    # Установка умерла (приложение снесли, токен отозван) — гасим токен
    invalid_token: bool = False
    error: str | None = None
    # Ключи или права не в порядке: слать дальше на эту платформу бессмысленно
    fatal: bool = False
