"""Шифрование для секретов чужих служб, которые приходится хранить в базе.

Сейчас это один refresh-токен Apple: он нужен ровно для одного — отозвать
доступ Sayr к Apple ID при удалении аккаунта (требование App Store).
Утёкший дамп базы (а дампы уезжают в бэкапы) не должен давать его в руки
открытым, поэтому в колонке лежит шифр Fernet, а ключ выводится из
SAYR_SECRET_KEY, которого в базе нет.

Сменили SAYR_SECRET_KEY — старые шифры больше не открываются: unseal
отдаёт None, и отзывать становится нечем. Это видно в журнале; следующий
вход с Apple положит новый токен.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

#: Своя соль: тот же secret_key подписывает cookie админки, и ключ
#: шифрования не должен совпадать ни с каким другим его применением
_PURPOSE = b"sayr-sealed-v1:"


def _fernet() -> Fernet:
    digest = hashlib.sha256(_PURPOSE + settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def seal(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def unseal(sealed: str | None) -> str | None:
    if not sealed:
        return None
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except InvalidToken:
        return None
