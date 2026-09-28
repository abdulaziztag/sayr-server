"""Что Sayr Admin пишет в группах. Два языка подряд: в группе люди
с приложением и на русском, и на узбекском, а знать его язык служба не может.

Первое сообщение утвердил владелец 27 сентября (спека попутчиков, часть 2).
"""

from datetime import date

from ..config import settings
from ..push.outbox import day_text
from ..typography import uz_display


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
