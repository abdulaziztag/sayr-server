"""Канал доставки кода: общий интерфейс для телеграма и будущей СМС.

Сервер не знает, чем именно доставлен код. Он просит канал «отправь код
на этот номер» и потом «проверь, верный ли код по этой заявке». Поэтому
добавление СМС (ждёт юрлица и согласования имени отправителя у операторов)
не тронет ни ручки, ни экраны — появится второй класс с теми же двумя
методами.

Код придумывает и проверяет сам канал. У нас его нет ни в базе, ни
в журналах: это самое дешёвое, что можно сделать для безопасности входа.
"""

from dataclasses import dataclass
from typing import Protocol

#: Итог отправки
SEND_OK = "sent"
#: У номера нет телеграма — единственный отказ, который человеку
#: показывается отдельным экраном, а не общей ошибкой
SEND_NO_TELEGRAM = "no_telegram"
SEND_FAILED = "failed"

#: Итог проверки кода. Значения совпадают с названиями у шлюза телеграма,
#: чтобы не переводить их дважды
CODE_VALID = "code_valid"
CODE_INVALID = "code_invalid"
CODE_MAX_ATTEMPTS = "code_max_attempts_exceeded"
CODE_EXPIRED = "expired"
#: Канал ответил чем-то незнакомым: считаем неудачей, но отличаем
#: от честного «код неверный», чтобы не сжигать попытку человека
CODE_UNKNOWN = "unknown"


@dataclass(slots=True)
class SendResult:
    status: str
    #: Номер заявки у канала. Наружу не отдаётся
    request_id: str | None = None
    #: Ответ канала как есть, для журнала: перечня ошибок в документации шлюза нет,
    #: и разбирать их придётся по живым отказам
    error: str | None = None


class CodeChannel(Protocol):
    """Умеет доставить код и проверить его."""

    name: str

    async def send(
        self,
        phone: str,
        *,
        ttl_sec: int,
        code_length: int,
        callback_url: str | None = None,
    ) -> SendResult: ...

    async def check(self, request_id: str, code: str) -> str: ...
