"""Лендинг: одна страница, чтобы во всех постах была одна ссылка.

Смысл ровно один — человеку с телефона не надо выбирать между двумя
ссылками на магазины. Кнопка сама определяет систему; для тех, у кого
определение не сработало, под ней лежат обе ссылки текстом.

Две страницы, а не переключатель на JavaScript: `/` по-русски, `/uz`
по-узбекски. Так ссылку можно дать сразу на нужном языке, и она
переживёт репост.

Адреса магазинов лежат в настройках и по умолчанию пусты: лендинг
поднимается раньше, чем приложения проходят ревью, и в этом окне
страница честно говорит «скоро», а не ведёт в никуда.
"""

import re

from fastapi import APIRouter, Depends, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..models import TesterSignup

router = APIRouter(tags=["landing"])

#: Нарочно нестрогая: отсечь мусор, не отвергая живые адреса.
#: Настоящая проверка одна — дойдёт ли письмо со ссылкой
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SUPPORT_EMAIL = "mannopov481@gmail.com"

RU = {
    "lang": "ru",
    "other": ("/uz", "Oʻzbekcha"),
    "title": "Sayr — куда сходить в горы Узбекистана",
    "tagline": "Куда сходить<br>в эти выходные",
    "lede": "Больше сотни мест рядом с Ташкентом: водопады, вершины, ущелья, "
            "пещеры и озёра. С треками, фотографиями и подсказкой, во сколько выезжать.",
    "install": "Установить",
    "soon": "Скоро в App Store и Google Play",
    "soon_note": "Приложение проходит проверку в магазинах. Загляните через несколько дней.",
    "ios": "Для iPhone",
    "android": "Для Android",
    "form_head": "Для Android — по приглашению",
    "form_lede": "Приложение в закрытом тестировании Google Play. Оставьте "
                 "почту от Google-аккаунта — добавим вас в тестировщики "
                 "и пришлём ссылку на установку, обычно в течение дня.",
    "form_placeholder": "почта Google-аккаунта",
    "form_button": "Получить ссылку",
    "form_note": "Почта нужна только для приглашения — никаких рассылок.",
    "form_ok": "Готово! Ссылка придёт на эту почту.",
    "form_fail": "Не отправилось — напишите нам письмом.",
    "blocks": [
        ("Во сколько выезжать",
         "Приложение берёт закат в координатах места и вычитает дорогу туда "
         "и обратно, ходовое время и запас. Получается час, позже которого "
         "выезжать не стоит."),
        ("Настоящие треки",
         "Записи GPS, которые прошли люди, а не нарисованные линии. Открываются "
         "в вашем навигаторе одним касанием. Отдельно — координаты начала тропы, "
         "чтобы доехать на машине."),
        ("Работает без связи",
         "Отметили выезд — описание, трек, фотографии и карта скачиваются заранее. "
         "В ущелье, где не ловит, всё открывается как ни в чём не бывало."),
        ("Без рекламы и регистрации",
         "Нет аккаунтов, нет рекламы, нет слежки. Геопозиция не запрашивается вовсе. "
         "Избранное и планы остаются на вашем телефоне."),
    ],
    "facts": [
        ("121", "место в каталоге"),
        ("12", "регионов Узбекистана"),
        ("GPX", "треки, записанные людьми"),
        ("0", "рекламы и аккаунтов"),
    ],
    "shots": [
        ("Каталог", "Сто с лишним мест с фотографиями, расстоянием "
                    "и честной оценкой сложности."),
        ("День по часам", "Во сколько выехать, сколько ехать, сколько идти "
                          "и когда садится солнце."),
        ("Карта", "Все места сразу — видно, что рядом, а что на выходные "
                  "с ночёвкой."),
    ],
    "honest": "Интерфейс приложения пока только на русском.",
    "privacy": "Конфиденциальность",
    "support": "Поддержка",
    "made": "Каталог собран вручную. Нашли неточность — напишите, поправим.",
}

UZ = {
    "lang": "uz",
    "other": ("/", "Русский"),
    "title": "Sayr — Oʻzbekiston togʻlariga qayerga borish",
    "tagline": "Bu dam olish kunlari<br>qayerga borish mumkin",
    "lede": "Toshkent yaqinida yuzdan ortiq joy: sharsharalar, choʻqqilar, daralar, "
            "gʻorlar va koʻllar. Treklar, suratlar va qachon yoʻlga chiqish kerakligi bilan.",
    "install": "Oʻrnatish",
    "soon": "Tez orada App Store va Google Playʼda",
    "soon_note": "Ilova doʻkonlarda tekshiruvdan oʻtmoqda. Bir necha kundan keyin qarang.",
    "ios": "iPhone uchun",
    "android": "Android uchun",
    "form_head": "Android uchun — taklif orqali",
    "form_lede": "Ilova Google Playʼda yopiq sinovda. Google akkauntingiz "
                 "pochtasini qoldiring — sizni sinovchilarga qoʻshamiz va "
                 "oʻrnatish havolasini yuboramiz, odatda bir kun ichida.",
    "form_placeholder": "Google akkaunt pochtasi",
    "form_button": "Havola olish",
    "form_note": "Pochta faqat taklif uchun kerak — hech qanday reklama yoʻq.",
    "form_ok": "Tayyor! Havola shu pochtaga keladi.",
    "form_fail": "Yuborilmadi — bizga xat yozing.",
    "blocks": [
        ("Qachon yoʻlga chiqish",
         "Ilova joyning koordinatalari boʻyicha quyoshning botishini oladi va "
         "yoʻl, yurish vaqti hamda zaxirani ayiradi. Shundan keyin chiqish "
         "tavsiya etilmaydigan vaqt kelib chiqadi."),
        ("Haqiqiy treklar",
         "Chizilgan chiziqlar emas, odamlar yurgan GPS yozuvlari. Bir teginishda "
         "navigatoringizda ochiladi. Alohida — mashinada yetib borish uchun "
         "soʻqmoq boshining koordinatalari."),
        ("Aloqasiz ishlaydi",
         "Chiqishni belgilasangiz, tavsif, trek, suratlar va xarita oldindan "
         "yuklab olinadi. Aloqa yoʻq darada ham hammasi ochilaveradi."),
        ("Reklamasiz va roʻyxatdan oʻtmasdan",
         "Akkaunt yoʻq, reklama yoʻq, kuzatuv yoʻq. Joylashuv umuman soʻralmaydi. "
         "Saralanganlar va rejalar telefoningizda qoladi."),
    ],
    "facts": [
        ("121", "joy katalogda"),
        ("12", "Oʻzbekiston mintaqasi"),
        ("GPX", "odamlar yozgan treklar"),
        ("0", "reklama va akkaunt"),
    ],
    "shots": [
        ("Katalog", "Yuzdan ortiq joy: suratlar, masofa va murakkablikning "
                    "halol bahosi."),
        ("Kun soatlab", "Qachon chiqish, qancha yurish va quyosh qachon botadi."),
        ("Xarita", "Barcha joylar birdan — nima yaqin, nimaga tunab borish "
                   "kerakligi koʻrinadi."),
    ],
    "honest": "Ilova interfeysi hozircha faqat rus tilida.",
    "privacy": "Maxfiylik",
    "support": "Yordam",
    "made": "Katalog qoʻlda yigʻilgan. Xatolik topsangiz — yozing, tuzatamiz.",
}

_PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{lede_plain}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{lede_plain}">
<meta property="og:image" content="/static/img/shot-catalog.jpg">
<meta name="theme-color" content="#F3EEE3" media="(prefers-color-scheme:light)">
<meta name="theme-color" content="#141714" media="(prefers-color-scheme:dark)">
<link rel="icon" href="/static/img/icon.png">
<style>
@font-face {{ font-family:Plex; src:url(/static/fonts/IBMPlexSans-Regular.ttf) format('truetype');
              font-weight:400; font-display:swap; }}
@font-face {{ font-family:Plex; src:url(/static/fonts/IBMPlexSans-SemiBold.ttf) format('truetype');
              font-weight:600; font-display:swap; }}
@font-face {{ font-family:PlexMono; src:url(/static/fonts/IBMPlexMono-Medium.ttf) format('truetype');
              font-weight:500; font-display:swap; }}
:root {{
  /* ink3 темнее приложенческого #8A8272, а кнопка темнее фирменной терракоты:
     на бумаге они давали 3.3:1 и 4.3:1, а мелкому тексту нужно 4.5:1.
     В приложении те же цвета лежат на крупных элементах, здесь — на подписях */
  --paper:#F3EEE3; --surface:#FBF8F1; --ink:#161A17; --ink2:#57524A; --ink3:#726A5C;
  --green:#2F5D3F; --terra:#C75B12; --cta:#B04E0C; --on-cta:#FFFFFF;
  --edge:#161A1714; --shadow:#161A1722;
  --step:clamp(3.5rem,9vw,6rem);
}}
@media (prefers-color-scheme:dark) {{
  /* На светлой терракоте тёмной темы белый текст давал 2.7:1 — на кнопке
     здесь тёмная подпись, как на янтарных плашках в самом приложении */
  :root {{ --paper:#141714; --surface:#1E221E; --ink:#EDEAE1; --ink2:#C6C1B5;
           --ink3:#8E897D; --green:#7FBF95; --terra:#E8843C; --cta:#E8843C;
           --on-cta:#1F0F06; --edge:#EDEAE11F; --shadow:#00000055; }}
}}
*,*::before,*::after {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink);
        font-family:Plex,ui-sans-serif,system-ui,sans-serif; line-height:1.55;
        -webkit-font-smoothing:antialiased; overflow-x:hidden; }}
a {{ color:var(--green); }}
:focus-visible {{ outline:2px solid var(--terra); outline-offset:3px; border-radius:4px; }}
.wrap {{ max-width:70rem; margin:0 auto; padding:clamp(1.25rem,4vw,2rem) clamp(1rem,5vw,2rem) 4rem; }}

/* Шапка */
header {{ display:flex; align-items:center; gap:.75rem; margin-bottom:clamp(2rem,6vw,3.5rem); }}
header img {{ width:44px; height:44px; border-radius:11px; }}
.name {{ font-weight:600; font-size:1.35rem; letter-spacing:-.01em; }}
/* Текстовые ссылки тоже пальцем нажимают: высота цели — 44px, даже когда
   сама надпись мелкая. Ниже этого промахиваются */
.langlink {{ margin-left:auto; display:inline-flex; align-items:center; min-height:44px;
             font-family:PlexMono,monospace; font-size:.72rem;
             letter-spacing:.1em; text-transform:uppercase; color:var(--ink3);
             text-decoration:none; }}
.langlink span {{ border-bottom:1px solid var(--edge); padding-bottom:3px; }}
.langlink:hover {{ color:var(--ink); }}

/* Экран-обложка: текст и телефон рядом */
.hero {{ display:grid; gap:clamp(2rem,6vw,4rem); align-items:center;
         grid-template-columns:1fr; }}
@media (min-width:56rem) {{ .hero {{ grid-template-columns:1.05fr .95fr; }} }}
h1 {{ font-size:clamp(2.1rem,6vw,3.6rem); line-height:1.06; margin:0 0 1rem;
      letter-spacing:-.03em; text-wrap:balance; }}
.lede {{ color:var(--ink2); font-size:clamp(1.02rem,2.2vw,1.18rem); max-width:36ch;
         margin:0 0 1.9rem; }}
.cta {{ display:flex; flex-wrap:wrap; gap:.9rem; align-items:center; margin-bottom:.85rem; }}
.btn {{ display:inline-flex; align-items:center; gap:.55rem; background:var(--cta);
        color:var(--on-cta); text-decoration:none; font-weight:600; font-size:1.05rem;
        padding:.9rem 2.1rem; border-radius:12px 12px 12px 26px; min-height:44px;
        transition:transform .18s ease, filter .18s ease; }}
.btn:hover {{ filter:brightness(1.07); transform:translateY(-1px); }}
.btn svg {{ width:19px; height:19px; }}
.soon {{ display:inline-block; font-family:PlexMono,monospace; font-size:.78rem;
         letter-spacing:.08em; text-transform:uppercase; color:var(--green);
         border:1px solid var(--green); border-radius:10px 10px 10px 22px; padding:.75rem 1.4rem; }}
.stores {{ display:flex; gap:1.1rem; font-size:.9rem; }}
.stores a {{ display:inline-flex; align-items:center; min-height:44px; }}
.note {{ color:var(--ink3); font-size:.85rem; margin:.4rem 0 0; }}

/* Телефон: рамка рисуется стилями, отдельной картинки для неё не нужно */
.phone {{ position:relative; width:min(17rem,72vw); margin:0 auto;
          background:var(--surface); padding:9px; border-radius:2.6rem;
          box-shadow:0 1px 0 var(--edge) inset, 0 22px 50px -18px var(--shadow);
          border:1px solid var(--edge); }}
.phone img {{ display:block; width:100%; height:auto; border-radius:2.05rem; }}
.hero .phone {{ transform:rotate(-1.4deg); }}

/* Полоса чисел */
.facts {{ display:grid; grid-template-columns:repeat(2,1fr); gap:1.2rem 1rem;
          margin-top:var(--step); padding-top:1.7rem; border-top:1px solid var(--edge); }}
@media (min-width:44rem) {{ .facts {{ grid-template-columns:repeat(4,1fr); }} }}
.fact b {{ display:block; font-family:PlexMono,monospace; font-size:1.5rem;
           font-weight:500; color:var(--green); letter-spacing:-.02em; }}
.fact span {{ font-size:.86rem; color:var(--ink2); }}

/* Галерея экранов */
.gallery {{ display:grid; gap:clamp(1.6rem,4vw,2.6rem); margin-top:var(--step);
            grid-template-columns:1fr; }}
@media (min-width:44rem) {{ .gallery {{ grid-template-columns:repeat(3,1fr); }} }}
.shot {{ text-align:center; }}
.shot h2 {{ font-size:1.02rem; margin:1.1rem 0 .35rem; letter-spacing:-.01em; }}
.shot p {{ margin:0 auto; color:var(--ink2); font-size:.9rem; max-width:26ch; }}
.shot .phone {{ width:min(15rem,66vw); }}

/* Что умеет */
/* 13rem, а не 15: при контейнере 70rem четыре колонки по 15rem с зазорами
   не помещались, и четвёртый блок оставался в строке один */
.blocks {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));
           gap:clamp(1.4rem,3vw,2.2rem); margin-top:var(--step);
           padding-top:1.7rem; border-top:1px solid var(--edge); }}
.block svg {{ width:22px; height:22px; color:var(--green); margin-bottom:.55rem; }}
.block h2 {{ font-size:1.02rem; margin:0 0 .4rem; letter-spacing:-.01em; }}
.block p {{ margin:0; color:var(--ink2); font-size:.94rem; }}

/* Форма закрытого теста Android */
.tform {{ background:var(--surface); border:1px solid var(--edge);
          border-radius:18px 18px 18px 38px; padding:clamp(1.3rem,3vw,2rem);
          margin-top:var(--step); max-width:38rem;
          box-shadow:0 14px 34px -22px var(--shadow); }}
.tform h2 {{ font-size:1.12rem; margin:0 0 .45rem; letter-spacing:-.015em; }}
.tform p {{ margin:0 0 1.1rem; color:var(--ink2); font-size:.94rem; max-width:44ch; }}
.tform .row {{ display:flex; flex-wrap:wrap; gap:.6rem; }}
.tform input[type=email] {{ flex:1 1 15rem; font:inherit; font-size:.95rem;
          padding:.8rem 1rem; border:1px solid var(--edge); border-radius:11px;
          background:var(--paper); color:var(--ink); min-width:0; min-height:44px; }}
.tform input[type=email]::placeholder {{ color:var(--ink3); }}
.tform button {{ font:inherit; font-weight:600; font-size:.95rem; cursor:pointer;
          background:var(--green); color:var(--paper); border:0; min-height:44px;
          padding:.8rem 1.5rem; border-radius:11px 11px 11px 22px;
          transition:filter .18s ease; }}
.tform button:hover {{ filter:brightness(1.1); }}
.tform .tnote {{ margin:.7rem 0 0; font-size:.82rem; color:var(--ink3); }}
.tform .tok {{ margin:.7rem 0 0; font-size:.94rem; color:var(--green); font-weight:600; }}
.hp {{ position:absolute; left:-9999px; width:1px; height:1px; overflow:hidden; }}

footer {{ margin-top:var(--step); padding-top:1.4rem; border-top:1px solid var(--edge);
          display:flex; flex-wrap:wrap; gap:1.2rem; align-items:center;
          color:var(--ink3); font-size:.85rem; }}
footer a {{ color:var(--ink3); display:inline-flex; align-items:center; min-height:44px; }}
footer .made {{ flex-basis:100%; margin:0; }}

/* Появление первого экрана — чистым CSS, без наблюдателя за прокруткой.
   Скрипт, от которого зависит видимость текста, — это не анимация,
   а способ показать пустую страницу тому, у кого он не отработал */
@keyframes rise {{ from {{ opacity:0; transform:translateY(12px); }} }}
.hero > div > * {{ animation:rise .5s ease-out backwards; }}
.hero > div > *:nth-child(2) {{ animation-delay:.06s; }}
.hero > div > *:nth-child(3) {{ animation-delay:.12s; }}
.hero .phone {{ animation:rise .6s .1s ease-out backwards; }}
@media (prefers-reduced-motion:reduce) {{
  .hero > div > *, .hero .phone {{ animation:none; }}
  .btn:hover {{ transform:none; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="/static/img/icon.png" alt="" width="44" height="44">
    <span class="name">Sayr</span>
    <a class="langlink" href="{other_href}"><span>{other_label}</span></a>
  </header>

  <section class="hero">
    <div>
      <h1>{tagline}</h1>
      <p class="lede">{lede}</p>
      {cta}
      <p class="note">{honest}</p>
    </div>
    <div class="phone">
      <picture>
        <source srcset="/static/img/shot-catalog.webp" type="image/webp">
        <img src="/static/img/shot-catalog.jpg" width="560" height="1159"
             alt="Каталог мест в приложении Sayr" fetchpriority="high">
      </picture>
    </div>
  </section>

  <section class="facts">{facts}</section>

  <section class="gallery">{gallery}</section>

  {tester_form}

  <section class="blocks">{blocks}</section>

  <footer>
    <a href="/privacy">{privacy}</a>
    <a href="/support">{support}</a>
    <a href="mailto:{email}">{email}</a>
    <p class="made">{made}</p>
  </footer>
</div>
{script}
</body>
</html>"""

# Иконки — контурные SVG, по одной на блок «что умеет». Не эмодзи: те
# зависят от шрифта системы и в вебе выглядят каждый раз по-разному
_ICONS = (
    # солнце над горизонтом — окно выезда
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4'
    'M18.4 5.6L17 7M7 17l-1.4 1.4"/></svg>',
    # трек — ломаная с точками
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M4 18l5-9 4 5 3-5 4 9"/><circle cx="4" cy="18" r="1.6"/>'
    '<circle cx="20" cy="18" r="1.6"/></svg>',
    # облако с чертой — офлайн
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round" aria-hidden="true"><path d="M7 18h10a4 4 0 000-8 '
    '5.5 5.5 0 00-10.3-1.4A3.6 3.6 0 007 18z"/><path d="M4 4l16 16"/></svg>',
    # замок — без рекламы и аккаунтов
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round" aria-hidden="true"><rect x="4.5" y="10" width="15" '
    'height="10" rx="2.5"/><path d="M8.5 10V7a3.5 3.5 0 017 0v3"/></svg>',
)

_ARROW = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
          'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
          '<path d="M12 4v13M6 11l6 6 6-6"/></svg>')

_SHOT_FILES = ("shot-catalog", "shot-detail", "shot-map")


# Кнопка сама выбирает магазин. Без JavaScript обе ссылки всё равно видны
# текстом ниже: страница обязана работать и с выключенными скриптами
_SCRIPT = """<script>
(function () {
  var a = document.getElementById('install');
  if (!a) return;
  var ua = navigator.userAgent || '';
  if (/iPhone|iPad|iPod/i.test(ua)) a.href = a.dataset.ios;
  else if (/Android/i.test(ua)) a.href = a.dataset.android;
})();
</script>"""


def _cta(t: dict) -> tuple[str, str]:
    ios, android = settings.app_store_url, settings.play_store_url
    if not (ios or android):
        return (
            f'<div class="cta"><span class="soon">{t["soon"]}</span></div>'
            f'<p class="note">{t["soon_note"]}</p>',
            "",
        )
    first = ios or android
    links = []
    if ios:
        links.append(f'<a href="{ios}">{t["ios"]}</a>')
    if android:
        links.append(f'<a href="{android}">{t["android"]}</a>')
    else:
        # В Google Play приложения ещё нет — андроидная половина кнопки
        # и текстовая ссылка ведут к форме закрытого теста ниже
        links.append(f'<a href="#android">{t["android"]}</a>')
    return (
        f'<div class="cta">'
        f'<a class="btn" id="install" href="{first}" '
        f'data-ios="{ios or android}" data-android="{android or "#android"}">'
        f'{_ARROW}{t["install"]}</a>'
        f'<span class="stores">{"".join(links)}</span></div>',
        _SCRIPT,
    )


def _tester_form(t: dict) -> str:
    """Форма закрытого теста. Живёт, только пока в Play нас нет: появится
    ссылка на магазин — форма исчезнет сама, без правок разметки.

    Обычный POST, работающий без JavaScript, плюс перехват сабмита ради
    ответа без перезагрузки. Скрытое поле-приманка отсеивает ботов,
    которые заполняют всё подряд: человек его не видит и не трогает.
    """
    if settings.play_store_url:
        return ""
    ok, fail = t["form_ok"], t["form_fail"]
    return f"""
  <form class="tform" id="android" method="post" action="/android-testers">
    <h2>{t["form_head"]}</h2>
    <p>{t["form_lede"]}</p>
    <div class="row">
      <input type="email" name="email" required maxlength="320"
             placeholder="{t["form_placeholder"]}" autocomplete="email">
      <button type="submit">{t["form_button"]}</button>
    </div>
    <input class="hp" type="text" name="website" tabindex="-1" autocomplete="off">
    <input type="hidden" name="lang" value="{t["lang"]}">
    <p class="tnote" data-note>{t["form_note"]}</p>
  </form>
  <script>
  (function () {{
    var f = document.getElementById('android');
    if (!f || !window.fetch) return;
    f.addEventListener('submit', function (e) {{
      e.preventDefault();
      fetch(f.action, {{ method: 'POST', body: new FormData(f),
                         headers: {{ 'Accept': 'application/json' }} }})
        .then(function (r) {{ if (!r.ok) throw 0; return r.json(); }})
        .then(function () {{
          f.querySelector('.row').style.display = 'none';
          var n = f.querySelector('[data-note]');
          n.textContent = {ok!r}; n.className = 'tok';
        }})
        .catch(function () {{
          f.querySelector('[data-note]').textContent = {fail!r};
        }});
    }});
  }})();
  </script>"""


def _render(t: dict) -> str:
    cta, script = _cta(t)
    tester_form = _tester_form(t)
    blocks = "".join(
        f'<div class="block">{icon}<h2>{head}</h2><p>{body}</p></div>'
        for icon, (head, body) in zip(_ICONS, t["blocks"])
    )
    facts = "".join(
        f'<div class="fact"><b>{value}</b><span>{caption}</span></div>'
        for value, caption in t["facts"]
    )
    gallery = "".join(
        f'<figure class="shot">'
        f'<div class="phone"><picture>'
        f'<source srcset="/static/img/{file}.webp" type="image/webp">'
        f'<img src="/static/img/{file}.jpg" width="560" height="1159"'
        f' alt="{head}" loading="lazy"></picture></div>'
        f'<h2>{head}</h2><p>{body}</p></figure>'
        for file, (head, body) in zip(_SHOT_FILES, t["shots"])
    )
    return _PAGE.format(
        lang=t["lang"],
        title=t["title"],
        tagline=t["tagline"],
        lede=t["lede"],
        lede_plain=t["lede"].replace('"', "&quot;"),
        other_href=t["other"][0],
        other_label=t["other"][1],
        cta=cta,
        tester_form=tester_form,
        facts=facts,
        gallery=gallery,
        honest=t["honest"],
        blocks=blocks,
        privacy=t["privacy"],
        support=t["support"],
        email=SUPPORT_EMAIL,
        made=t["made"],
        script=script,
    )


_THANKS = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sayr</title>
<style>body{{margin:0;background:#F3EEE3;color:#161A17;
font-family:ui-sans-serif,system-ui,sans-serif;display:grid;place-items:center;
min-height:100vh;padding:2rem}}
@media (prefers-color-scheme:dark){{body{{background:#141714;color:#EDEAE1}}}}
main{{max-width:26rem;text-align:center}}h1{{font-size:1.4rem}}
a{{color:#2F5D3F}}</style></head>
<body><main><h1>{head}</h1><p>{body}</p><p><a href="{back}">{back_label}</a></p></main>
</body></html>"""

_THANKS_T = {
    "ru": ("Готово!", "Ссылка на установку придёт на эту почту, обычно в течение дня.",
           "/", "На главную"),
    "uz": ("Tayyor!", "Oʻrnatish havolasi shu pochtaga keladi, odatda bir kun ichida.",
           "/uz", "Bosh sahifaga"),
}


@router.post("/android-testers")
async def android_tester_signup(
    email: str = Form(..., max_length=320),
    lang: str = Form("ru"),
    website: str = Form(""),
    accept: str = Header("", alias="Accept"),
    session: AsyncSession = Depends(get_session),
):
    """Заявка на закрытый тест Android.

    Ответ одинаково спокойный на всё, кроме явно кривого адреса: и на новый,
    и на повторный, и на пойманного приманкой бота. Разный ответ выдал бы
    наружу, какие адреса лежат в базе, а боту — что его раскусили.
    """
    lang = lang if lang in ("ru", "uz") else "ru"
    address = email.strip().lower()
    if not _EMAIL.fullmatch(address):
        raise HTTPException(422, "это не похоже на почту")

    # Поле-приманка заполнено — человек его не видит, значит это бот.
    if not website:
        await session.execute(
            insert(TesterSignup)
            .values(email=address, lang=lang)
            .on_conflict_do_nothing(index_elements=["email"])
        )
        await session.commit()

    if "application/json" in accept:
        return JSONResponse({"ok": True})
    head, body, back, back_label = _THANKS_T[lang]
    return HTMLResponse(
        _THANKS.format(lang=lang, head=head, body=body, back=back, back_label=back_label)
    )


@router.get("/", response_class=HTMLResponse)
async def landing() -> str:
    return _render(RU)


@router.get("/uz", response_class=HTMLResponse)
async def landing_uz() -> str:
    return _render(UZ)
