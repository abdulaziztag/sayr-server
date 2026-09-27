"""Ссылки sayr.info/p/… и /r/…, когда система не открыла приложение сама.

С приложением ссылку перехватывает система: iOS — по
apple-app-site-association, Android — по assetlinks.json (room_page.py).
Сюда доходят, когда приложения нет или подпись ещё не сверена. Телефон
тогда уходит дальше мимо страницы: iPhone — в App Store, Android — через
intent:// в приложение, а если его нет — в Google Play. Компьютер и роботы
превью получают страницу: Telegram и WhatsApp собирают превью ссылки
из её og-тегов.

В магазин ведём через /go/{платформа}?from=… (landing.py): переход
записывается с меткой, и видно, сколько людей пришло в магазин со ссылок.
"""

import re
from html import escape
from urllib.parse import quote

from ..config import settings

# Роботы превью и поисковики: им нужна страница с og-тегами, а не магазин.
# У смартфонного Googlebot в строке есть и Android — робота проверяем первым.
# iMessage представляется facebookexternalhit, VK — vkShare
_ROBOT = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|vkshare", re.I)
_IOS = re.compile(r"iPhone|iPad|iPod")

_STORE_LABELS = {
    "ru": {"ios": "Скачать в App Store", "android": "Скачать в Google Play"},
    "uz": {"ios": "App Store'dan yuklab olish", "android": "Google Play'dan yuklab olish"},
}


def _go(platform: str, mark: str) -> str:
    return f"{settings.public_url}/go/{platform}?from={mark}"


def phone_redirect(user_agent: str | None, deep_link: str, mark: str) -> str | None:
    """Куда увести телефон мимо страницы; None — показать страницу.

    `deep_link` — путь в приложении без схемы: `place/<slug>`. `mark` —
    метка перехода в магазин для статистики: `share`, `invite`.
    """
    ua = user_agent or ""
    if not ua or _ROBOT.search(ua):
        return None
    if _IOS.search(ua):
        if not settings.ios_store_redirect or not settings.app_store_url:
            return None
        return _go("ios", mark)
    if "Android" not in ua or not settings.play_store_url:
        return None
    # Встроенный браузер приложения (WebView) intent:// не понимает —
    # сразу в магазин
    if "; wv)" in ua:
        return _go("android", mark)
    # Chrome и браузеры на нём: приложение стоит — откроется на нужном
    # экране по sayr://; нет — браузер уйдёт на запасной адрес, в магазин
    fallback = quote(_go("android", mark), safe="")
    return (
        f"intent://{deep_link}#Intent;scheme=sayr;package={settings.android_package};"
        f"S.browser_fallback_url={fallback};end"
    )


def store_buttons(lang: str, mark: str) -> str:
    """Кнопки магазинов для компьютера и тех, кого не увело само"""
    labels = _STORE_LABELS.get(lang, _STORE_LABELS["ru"])
    buttons = []
    if settings.app_store_url:
        buttons.append(f'<a class="btn store" href="{escape(_go("ios", mark))}">{labels["ios"]}</a>')
    if settings.play_store_url:
        buttons.append(
            f'<a class="btn store" href="{escape(_go("android", mark))}">{labels["android"]}</a>'
        )
    return "\n  ".join(buttons)


def smart_banner(url: str) -> str:
    """Баннер Safari «Открыть / Загрузить» — для iPad, который представляется
    компьютером и в магазин сам не уходит. Ссылка уезжает в приложение"""
    found = re.search(r"id(\d+)", settings.app_store_url or "")
    if not found:
        return ""
    return (
        f'<meta name="apple-itunes-app" content="app-id={found.group(1)}, '
        f'app-argument={escape(url)}">'
    )
