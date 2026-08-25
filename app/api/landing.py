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

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from ..config import settings

router = APIRouter(tags=["landing"])

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
<meta property="og:image" content="/static/img/shot1.jpg">
<link rel="icon" href="/static/img/icon.png">
<style>
@font-face {{ font-family:Plex; src:url(/static/fonts/IBMPlexSans-Regular.ttf) format('truetype');
              font-weight:400; font-display:swap; }}
@font-face {{ font-family:Plex; src:url(/static/fonts/IBMPlexSans-SemiBold.ttf) format('truetype');
              font-weight:600; font-display:swap; }}
@font-face {{ font-family:PlexMono; src:url(/static/fonts/IBMPlexMono-Medium.ttf) format('truetype');
              font-weight:500; font-display:swap; }}
:root {{
  --paper:#F3EEE3; --surface:#FBF8F1; --ink:#161A17; --ink2:#57524A; --ink3:#8A8272;
  --green:#2F5D3F; --terra:#C75B12; --edge:#161A1714;
}}
@media (prefers-color-scheme:dark) {{
  :root {{ --paper:#141714; --surface:#1E221E; --ink:#EDEAE1; --ink2:#C6C1B5;
           --ink3:#8E897D; --green:#7FBF95; --terra:#E8843C; --edge:#EDEAE11F; }}
}}
*,*::before,*::after {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink);
        font-family:Plex,ui-sans-serif,system-ui,sans-serif; line-height:1.55;
        -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:64rem; margin:0 auto; padding:clamp(1.5rem,5vw,3.5rem) clamp(1rem,5vw,2rem) 4rem; }}
header {{ display:flex; align-items:center; gap:.75rem; margin-bottom:clamp(2rem,7vw,4rem); }}
header img {{ width:44px; height:44px; border-radius:11px; }}
.name {{ font-weight:600; font-size:1.35rem; letter-spacing:-.01em; }}
.langlink {{ margin-left:auto; font-family:PlexMono,monospace; font-size:.72rem;
             letter-spacing:.1em; text-transform:uppercase; color:var(--ink3);
             text-decoration:none; border-bottom:1px solid var(--edge); padding-bottom:2px; }}
.langlink:hover {{ color:var(--ink); }}
h1 {{ font-size:clamp(2rem,6.5vw,3.4rem); line-height:1.08; margin:0 0 1rem;
      letter-spacing:-.025em; text-wrap:balance; }}
.lede {{ color:var(--ink2); font-size:clamp(1rem,2.3vw,1.15rem); max-width:34ch; margin:0 0 2rem; }}
.cta {{ display:flex; flex-wrap:wrap; gap:.9rem; align-items:center; margin-bottom:.85rem; }}
.btn {{ display:inline-block; background:var(--terra); color:#fff; text-decoration:none;
        font-weight:600; font-size:1.05rem; padding:.85rem 2.2rem; border-radius:12px 12px 12px 26px; }}
.btn:hover {{ filter:brightness(1.06); }}
.soon {{ display:inline-block; font-family:PlexMono,monospace; font-size:.78rem;
         letter-spacing:.08em; text-transform:uppercase; color:var(--green);
         border:1px solid var(--green); border-radius:10px 10px 10px 22px; padding:.75rem 1.4rem; }}
.stores {{ display:flex; gap:1.1rem; font-size:.86rem; }}
.stores a {{ color:var(--green); }}
.note {{ color:var(--ink3); font-size:.85rem; margin:.4rem 0 0; }}
.shots {{ display:grid; grid-template-columns:repeat(3,1fr); gap:clamp(.6rem,2vw,1.4rem);
          margin:clamp(2.5rem,8vw,4.5rem) 0; }}
.shots img {{ width:100%; height:auto; border-radius:16px 16px 16px 34px;
              border:1px solid var(--edge); display:block; }}
@media (max-width:560px) {{ .shots {{ grid-template-columns:1fr 1fr; }}
                            .shots img:last-child {{ display:none; }} }}
.blocks {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));
           gap:clamp(1.2rem,3vw,2rem); }}
.block h2 {{ font-size:1.02rem; margin:0 0 .45rem; letter-spacing:-.01em; }}
.block p {{ margin:0; color:var(--ink2); font-size:.94rem; }}
footer {{ margin-top:clamp(3rem,9vw,5rem); padding-top:1.4rem; border-top:1px solid var(--edge);
          display:flex; flex-wrap:wrap; gap:1.2rem; align-items:center;
          color:var(--ink3); font-size:.85rem; }}
footer a {{ color:var(--ink3); }}
footer .made {{ flex-basis:100%; margin:0; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="/static/img/icon.png" alt="">
    <span class="name">Sayr</span>
    <a class="langlink" href="{other_href}">{other_label}</a>
  </header>

  <h1>{tagline}</h1>
  <p class="lede">{lede}</p>

  {cta}
  <p class="note">{honest}</p>

  <div class="shots">
    <img src="/static/img/shot1.jpg" alt="" loading="lazy">
    <img src="/static/img/shot2.jpg" alt="" loading="lazy">
    <img src="/static/img/shot3.jpg" alt="" loading="lazy">
  </div>

  <div class="blocks">{blocks}</div>

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
    return (
        f'<div class="cta">'
        f'<a class="btn" id="install" href="{first}" '
        f'data-ios="{ios or android}" data-android="{android or ios}">{t["install"]}</a>'
        f'<span class="stores">{"".join(links)}</span></div>',
        _SCRIPT,
    )


def _render(t: dict) -> str:
    cta, script = _cta(t)
    blocks = "".join(
        f'<div class="block"><h2>{head}</h2><p>{body}</p></div>' for head, body in t["blocks"]
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
        honest=t["honest"],
        blocks=blocks,
        privacy=t["privacy"],
        support=t["support"],
        email=SUPPORT_EMAIL,
        made=t["made"],
        script=script,
    )


@router.get("/", response_class=HTMLResponse)
async def landing() -> str:
    return _render(RU)


@router.get("/uz", response_class=HTMLResponse)
async def landing_uz() -> str:
    return _render(UZ)
