"""Что Sayr Admin пишет в группах. Два языка подряд: в группе люди
с приложением и на русском, и на узбекском, а знать его язык служба не может.

Первое сообщение утвердил владелец 27 сентября (спека попутчиков, часть 2).
Заметки о людях — только имя, как его видят в комнате: ни номера, ни ника
Telegram, ни слова о блоках и жалобах (владелец, 01–02.10).
"""

from datetime import date

from ..config import settings
from ..models import User
from ..push.outbox import day_text
from ..typography import uz_display

#: Кого назвать, когда имени в анкете нет
NOBODY = ("Пользователь Sayr", "Sayr foydalanuvchisi")


def title(place: str, day: date) -> str:
    return f"Sayr · {place} · {day:%d.%m}"[:128]


def about(place: str, day: date, slug: str) -> str:
    return f"Поход из Sayr: {place}, {day_text(day, 'ru')}. {settings.public_url}/p/{slug}"[:255]


def first_message(place: str, place_uz: str, day: date, slug: str) -> str:
    return uz_display(
        f"Привет! Это группа похода на {place}, {day_text(day, 'ru')}. Её создал Sayr, "
        "чтобы вам было где договориться. Я — Sayr Admin: собираю отсюда, что вы "
        "рассказываете о тропе и месте, чтобы данные в приложении были точнее. "
        "Организатор может убрать меня командой /leave.\n"
        f"Место в Sayr: {settings.public_url}/p/{slug}\n\n"
        f"Salom! Bu {place_uz or place} sayohati guruhi, {day_text(day, 'uz')}. "
        "Uni Sayr yaratdi — kelishib olishingiz uchun. Men Sayr Admin: soʻqmoq va joy "
        "haqida yozganlaringizni yigʻaman, ilovadagi maʼlumotlar aniqroq boʻlishi uchun. "
        "Tashkilotchi meni /leave buyrugʻi bilan chiqarib yuborishi mumkin."
    )


def person(user: User) -> dict:
    """Что заметке о человеке нужно знать — в задание, пока строка участия
    цела: имя, как его видят в комнате, и пол — «вышел» или «вышла»"""
    return {"name": user.first_name, "gender": user.gender.value if user.gender else None}


def _who(name: str | None) -> tuple[str, str]:
    name = (name or "").strip()
    return (name, name) if name else NOBODY


def request_note(name: str, code: str) -> str:
    """Кто-то попросился в комнату. Ссылка — /j/ по коду комнаты: приложения
    открывают по ней ту же комнату, что из пуша, и организатор решает там.
    Не /r/: та — по секрету «Позвать своих», пускает без одобрения, и в
    истории группы её мог бы взять кто угодно"""
    ru, uz = _who(name)
    return uz_display(
        f"{ru} просится в поход. Решает организатор — в Sayr.\n\n"
        f"{uz} sayohatga qoʻshilmoqchi. Tashkilotchi Sayr ilovasida hal qiladi.\n\n"
        f"{settings.public_url}/j/{code}"
    )


def request_approved(name: str) -> str:
    """Та же заметка после одобрения: ссылка больше не нужна"""
    ru, uz = _who(name)
    return uz_display(f"{ru} теперь в походе.\n\n{uz} endi sayohatda.")


def left_note(reason: str, name: str | None, gender: str | None) -> str:
    """Человека убрали из группы, потому что в комнате его больше нет.
    «Вышел сам» — только когда вышел сам; удалил ли его организатор,
    заблокировал или запретили попутчиков — группе одно: не в походе"""
    ru, uz = _who(name)
    if reason == "left":
        verb = "вышла" if gender == "female" else "вышел"
        return uz_display(
            f"{ru} {verb} из комнаты в Sayr.\n\n{uz} Sayr ilovasidagi xonadan chiqdi."
        )
    return uz_display(f"{ru} больше не в походе.\n\n{uz} endi sayohatda emas.")


TEXTS = {
    "cancelled": uz_display(
        "Организатор отменил поход. Группа остаётся — договаривайтесь, если идёте всё равно.\n\n"
        "Tashkilotchi sayohatni bekor qildi. Guruh qoladi — baribir borsangiz, shu yerda kelishing."
    ),
    "farewell": uz_display(
        "Спасибо, что ходили с Sayr! Я выхожу из группы — она остаётся вашей.\n\n"
        "Sayr bilan borganingiz uchun rahmat! Men guruhdan chiqaman — u sizniki boʻlib qoladi."
    ),
    "bye": uz_display(
        "Хорошо, выхожу. Всё, что успел сохранить из этой группы, удалено.\n\n"
        "Xoʻp, chiqaman. Bu guruhdan saqlaganlarimning hammasi oʻchirildi."
    ),
}
