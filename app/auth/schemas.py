"""Что сервер рассказывает приложению о человеке."""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from ..models import Gender, User


class UserOut(BaseModel):
    id: int
    #: Свой номер человек и так знает — показываем как есть, целиком.
    #: Попутчикам он не уедет никогда: у них своя, урезанная карточка
    phone: str
    first_name: str = ""
    last_name: str = ""
    gender: Gender | None = None
    birth_year: int | None = None
    telegram_username: str | None = None
    avatar_url: str | None = None
    #: Анкету показываем один раз после входа; дальше о ней напоминает
    #: карточка в Профиле, а не экран поверх всего
    profile_filled: bool = False
    created_at: datetime | None = None

    @classmethod
    def of(cls, user: User) -> "UserOut":
        name = Path(user.avatar.name).name if user.avatar else None
        return cls(
            id=user.id,
            phone=user.phone,
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            gender=user.gender,
            birth_year=user.birth_year,
            # Пустая строка осталась от прежней версии ручки — это тоже «нет ника»
            telegram_username=user.telegram_username or None,
            avatar_url=f"/media/avatars/{name}" if name else None,
            profile_filled=user.profile_filled_at is not None,
            created_at=user.created_at,
        )
