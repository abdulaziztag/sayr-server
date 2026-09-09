"""Форма обратной связи по местам: /report.

Каталог собран вручную — по чужим отчётам, трекам с часов и звонкам
в нацпарки. Часть цифр в нём приблизительная, и точнее всего их знает
тот, кто вернулся из этого места вчера. Форма — короткий путь от «а тут
неправда» до строки в очереди, которую владелец разбирает в админке.

Страниц две, как и у лендинга: `/report` по-русски, `/uz/report`
по-узбекски. Со страницы места (`/p/{slug}`) приходят с `?place=slug` —
тогда место уже подставлено и человеку остаётся сказать, что не так.

Работает без JavaScript: обычный POST отвечает страницей благодарности.
Скрипт нужен только чтобы ответить, не перезагружая страницу.
"""

import time
from html import escape

from fastapi import (APIRouter, Depends, File, Form, Header, HTTPException,
                     Query, Request, UploadFile)
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..models import Place, PlaceReport, PlaceReportFile
from ..reports import CATEGORY, TOPIC_CODES, TOPICS, normalize_contact
from ..schemas import Lang, pick
from ..services import attachments

router = APIRouter(tags=["report"])

SUPPORT_EMAIL = "mannopov481@gmail.com"

RU = {
    "lang": "ru",
    "other": ("/uz/report", "Oʻzbekcha"),
    "title": "Нашли неточность — Sayr",
    "h1": "Нашли неточность?",
    "lede": "Каталог собран вручную: по отчётам, чужим трекам и звонкам "
            "в нацпарки. Часть цифр в нём приблизительная, и точнее всего "
            "их знает тот, кто вернулся оттуда вчера. Напишите, что "
            "не сошлось, — поправим.",
    "place_label": "Какое место",
    "place_ph": "начните вводить название",
    "place_hint": "Нет в списке — впишите словами, как называете сами.",
    "topics_label": "Что не так",
    "topics_hint": "Отметьте всё, что не сошлось.",
    "comment_label": "Как правильно",
    "comment_ph": "Шли 6 часов вместо четырёх: последний час — по осыпи. "
                  "И трек уходит через частный сад, обходить надо левее.",
    "contact_label": "Телеграм для связи",
    "contact_ph": "@username",
    "contact_hint": "Не обязательно, но если понадобится уточнить — писать "
                    "будет некуда. Ник виден только нам.",
    "files_label": "Фото или файл",
    "files_button": "Выбрать файлы",
    "files_hint": "Снимок развилки, скриншот часов, свой трек GPX — что угодно, "
                  "что объясняет быстрее слов. До четырёх файлов по 8 МБ.",
    "files_types": "Фото, PDF или GPX",
    "too_many": "Больше четырёх файлов за раз не принимаем.",
    "too_big": "«{name}» тяжелее 8 МБ. Пришлите кадр поменьше — читаемости хватит.",
    "too_heavy": "Вместе файлы тяжелее 16 МБ. Пришлите самое важное.",
    "bad_type": "«{name}» не открылось. Подойдут фото (JPEG, PNG, HEIC, WebP), PDF и GPX.",
    "submit": "Отправить",
    "sending": "Отправляем…",
    "empty": "Отметьте тему или напишите, что не так.",
    "ok_head": "Дошло, спасибо",
    "ok_body": "Разберём и поправим. Если оставили телеграм — напишем, "
               "когда понадобятся подробности.",
    "fail": "Не отправилось. Попробуйте ещё раз или напишите на почту.",
    "too_often": "Слишком много заявок с этого адреса за час. Напишите на почту — разберёмся.",
    "keep": "Что написали, контакт и файлы храним, пока разбираем заявку, "
            "потом удаляем. Больше они ни для чего не нужны, и никуда, "
            "кроме нас, не попадают.",
    "back": "На главную",
    "back_href": "/",
}

UZ = {
    "lang": "uz",
    "other": ("/report", "Русский"),
    "title": "Xatolik topdingizmi — Sayr",
    "h1": "Xatolik topdingizmi?",
    "lede": "Katalog qoʻlda yigʻilgan: hisobotlar, boshqalarning treklari "
            "va milliy bogʻlarga qoʻngʻiroqlar boʻyicha. Ayrim raqamlar "
            "taxminiy, ularni kecha oʻsha yerdan qaytgan odam aniqroq "
            "biladi. Nima toʻgʻri kelmaganini yozing — tuzatamiz.",
    "place_label": "Qaysi joy",
    "place_ph": "nomini tera boshlang",
    "place_hint": "Roʻyxatda yoʻqmi — oʻzingiz ataydigan nom bilan yozing.",
    "topics_label": "Nima notoʻgʻri",
    "topics_hint": "Toʻgʻri kelmagan hammasini belgilang.",
    "comment_label": "Qanday boʻlishi kerak",
    "comment_ph": "Toʻrt emas, olti soat yurdik: oxirgi soati — toshloq "
                  "boʻylab. Trek esa xususiy bogʻdan oʻtadi, chapdan "
                  "aylanib oʻtish kerak.",
    "contact_label": "Bogʻlanish uchun Telegram",
    "contact_hint": "Majburiy emas, lekin aniqlashtirish kerak boʻlsa, "
                    "yozadigan joy qolmaydi. Nikni faqat biz koʻramiz.",
    "contact_ph": "@username",
    "files_label": "Surat yoki fayl",
    "files_button": "Fayl tanlash",
    "files_hint": "Ayrilish surati, soat ekrani, oʻzingiz yozgan GPX trek — "
                  "soʻzdan tez tushuntiradigan hamma narsa. Toʻrttagacha fayl, "
                  "har biri 8 MB gacha.",
    "files_types": "Surat, PDF yoki GPX",
    "too_many": "Bir vaqtda toʻrttadan koʻp fayl qabul qilinmaydi.",
    "too_big": "«{name}» 8 MB dan ogʻir. Kichikroq surat yuboring — oʻqishga yetadi.",
    "too_heavy": "Fayllar birgalikda 16 MB dan ogʻir. Eng kerakligini yuboring.",
    "bad_type": "«{name}» ochilmadi. Surat (JPEG, PNG, HEIC, WebP), PDF va GPX boʻladi.",
    "submit": "Yuborish",
    "sending": "Yuborilmoqda…",
    "empty": "Mavzuni belgilang yoki nima notoʻgʻriligini yozing.",
    "ok_head": "Yetib keldi, rahmat",
    "ok_body": "Koʻrib chiqamiz va tuzatamiz. Telegram qoldirgan boʻlsangiz — "
               "tafsilot kerak boʻlganda yozamiz.",
    "fail": "Yuborilmadi. Yana urinib koʻring yoki pochtaga yozing.",
    "too_often": "Bir soatda bu manzildan juda koʻp ariza keldi. Pochtaga yozing — koʻrib chiqamiz.",
    "keep": "Yozganingiz, kontaktingiz va fayllaringiz ariza koʻrib chiqilguncha "
            "saqlanadi, keyin oʻchiriladi. Ular boshqa hech narsaga kerak emas "
            "va bizdan boshqa hech kimga koʻrinmaydi.",
    "back": "Bosh sahifaga",
    "back_href": "/uz",
}

_T = {"ru": RU, "uz": UZ}

_PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="robots" content="noindex">
<link rel="icon" href="/static/img/icon.png">
<link rel="preload" href="/static/fonts/IBMPlexSans-Regular.woff2" as="font"
      type="font/woff2" crossorigin>
<style>
@font-face {{ font-family:Plex; font-weight:400; font-display:optional;
              src:url(/static/fonts/IBMPlexSans-Regular.woff2) format('woff2'),
                  url(/static/fonts/IBMPlexSans-Regular.ttf) format('truetype'); }}
@font-face {{ font-family:Plex; font-weight:600; font-display:optional;
              src:url(/static/fonts/IBMPlexSans-SemiBold.woff2) format('woff2'),
                  url(/static/fonts/IBMPlexSans-SemiBold.ttf) format('truetype'); }}
@font-face {{ font-family:PlexMono; font-weight:500; font-display:optional;
              src:url(/static/fonts/IBMPlexMono-Medium.woff2) format('woff2'),
                  url(/static/fonts/IBMPlexMono-Medium.ttf) format('truetype'); }}
/* Те же токены, что на лендинге: ink3 и кнопка темнее приложенческих —
   мелкому тексту на бумаге нужно 4.5:1 */
:root {{
  --paper:#F3EEE3; --surface:#FBF8F1; --ink:#161A17; --ink2:#57524A; --ink3:#726A5C;
  --green:#2F5D3F; --terra:#C75B12; --cta:#B04E0C; --on-cta:#FFFFFF;
  --edge:#161A1714; --line:#161A1726; --shadow:#161A1722;
  --t-fast:.16s; --e-sharp:cubic-bezier(.4,0,.2,1);
}}
@media (prefers-color-scheme:dark) {{
  :root {{ --paper:#141714; --surface:#1E221E; --ink:#EDEAE1; --ink2:#C6C1B5;
           --ink3:#8E897D; --green:#7FBF95; --terra:#E8843C; --cta:#E8843C;
           --on-cta:#1F0F06; --edge:#EDEAE11F; --line:#EDEAE133; --shadow:#00000055; }}
}}
*,*::before,*::after {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink);
        font-family:Plex,ui-sans-serif,system-ui,sans-serif; line-height:1.55;
        -webkit-font-smoothing:antialiased; }}
a {{ color:var(--green); }}
:focus-visible {{ outline:2px solid var(--terra); outline-offset:3px; border-radius:6px; }}
.wrap {{ max-width:44rem; margin:0 auto; padding:clamp(1.25rem,4vw,2rem) clamp(1rem,5vw,2rem) 4rem; }}

header {{ display:flex; align-items:center; gap:.75rem; margin-bottom:clamp(1.6rem,5vw,2.4rem); }}
header img {{ width:40px; height:40px; border-radius:10px; }}
.name {{ font-weight:600; font-size:1.2rem; letter-spacing:-.01em;
         color:var(--ink); text-decoration:none; }}
.langlink {{ margin-left:auto; display:inline-flex; align-items:center; min-height:44px;
             font-family:PlexMono,monospace; font-size:.72rem; letter-spacing:.1em;
             text-transform:uppercase; color:var(--ink3); text-decoration:none; }}
.langlink span {{ border-bottom:1px solid var(--edge); padding-bottom:3px; }}
.langlink:hover {{ color:var(--ink); }}

h1 {{ font-size:clamp(1.7rem,5vw,2.4rem); line-height:1.1; margin:0 0 .7rem;
      letter-spacing:-.025em; text-wrap:balance; }}
.lede {{ color:var(--ink2); margin:0 0 clamp(1.6rem,4vw,2.2rem); max-width:44ch; }}

form {{ background:var(--surface); border:1px solid var(--edge);
        border-radius:18px 18px 18px 38px; padding:clamp(1.2rem,3.5vw,2rem);
        box-shadow:0 14px 34px -22px var(--shadow); }}
.field {{ margin-bottom:1.6rem; }}
.field:last-of-type {{ margin-bottom:1.2rem; }}
.lab {{ display:block; font-weight:600; font-size:1rem; margin-bottom:.5rem; }}
.hint {{ margin:.5rem 0 0; font-size:.84rem; color:var(--ink3); }}
input[type=text], textarea {{ width:100%; font:inherit; font-size:1rem;
        padding:.75rem .9rem; border:1px solid var(--line); border-radius:12px;
        background:var(--paper); color:var(--ink); min-height:48px;
        transition:border-color var(--t-fast) var(--e-sharp); }}
input[type=text]:focus, textarea:focus {{ border-color:var(--green); }}
input::placeholder, textarea::placeholder {{ color:var(--ink3); }}
textarea {{ min-height:8.5rem; resize:vertical; line-height:1.5; }}

/* Темы — флажки, нарисованные как наклейки. Сам input не прячем
   насовсем: он остаётся в потоке для клавиатуры и озвучки */
.chips {{ display:flex; flex-wrap:wrap; gap:.5rem; }}
.chip {{ position:relative; display:inline-flex; }}
.chip input {{ position:absolute; opacity:0; width:100%; height:100%; margin:0;
               cursor:pointer; }}
.chip span {{ display:inline-flex; align-items:center; min-height:40px;
              padding:.45rem .95rem; border:1px solid var(--line); border-radius:20px;
              font-size:.92rem; color:var(--ink2); background:var(--paper);
              transition:background var(--t-fast) var(--e-sharp),
                         border-color var(--t-fast) var(--e-sharp),
                         color var(--t-fast) var(--e-sharp); }}
.chip input:checked + span {{ background:var(--green); border-color:var(--green);
                              color:var(--surface); font-weight:600; }}
.chip input:focus-visible + span {{ outline:2px solid var(--terra); outline-offset:2px; }}
@media (hover:hover) {{ .chip:hover span {{ border-color:var(--green); }} }}

/* Поле файлов. Настоящий input лежит поверх рамки прозрачным слоем:
   так остаются и клик по всей площадке, и клавиатура, и «выбрать файл»
   от озвучки — а рисуем мы своё */
.drop {{ position:relative; display:flex; align-items:center; gap:.7rem;
         min-height:56px; padding:.8rem 1rem; border:1px dashed var(--line);
         border-radius:14px; background:var(--paper); color:var(--ink2);
         cursor:pointer; transition:border-color var(--t-fast) var(--e-sharp),
                                    color var(--t-fast) var(--e-sharp); }}
.drop input {{ position:absolute; inset:0; width:100%; height:100%; opacity:0;
               cursor:pointer; }}
.drop svg {{ flex:none; width:20px; height:20px; stroke:currentColor;
             stroke-width:1.6; fill:none; stroke-linecap:round;
             stroke-linejoin:round; }}
.drop b {{ font-weight:600; color:var(--ink); white-space:nowrap; }}
.drop em {{ font-style:normal; margin-left:auto; font-family:PlexMono,monospace;
            font-size:.72rem; letter-spacing:.08em; text-transform:uppercase;
            white-space:nowrap; color:var(--ink3); }}
/* На узком экране приписка про типы ломала кнопку на две строки. Она и
   не нужна: подсказка под полем называет то же самое словами, а выбор
   в самом проводнике уже сужен атрибутом accept */
@media (max-width:30rem) {{ .drop em {{ display:none; }} }}
.drop input:focus-visible + .face {{ outline:2px solid var(--terra);
                                     outline-offset:4px; border-radius:10px; }}
.face {{ display:flex; align-items:center; gap:.7rem; width:100%; }}
@media (hover:hover) {{ .drop:hover {{ border-color:var(--green); color:var(--ink); }} }}

.picked {{ list-style:none; margin:.7rem 0 0; padding:0; display:grid; gap:.45rem; }}
.picked li {{ display:flex; align-items:baseline; gap:.6rem; font-size:.9rem;
              color:var(--ink2); }}
.picked li span {{ flex:1; overflow:hidden; text-overflow:ellipsis;
                   white-space:nowrap; }}
.picked li em {{ font-style:normal; font-family:PlexMono,monospace;
                 font-size:.76rem; color:var(--ink3); }}

.send {{ display:flex; flex-wrap:wrap; align-items:center; gap:1rem; }}
button {{ font:inherit; font-weight:600; font-size:1rem; cursor:pointer;
          background:var(--cta); color:var(--on-cta); border:0; min-height:48px;
          padding:.8rem 1.8rem; border-radius:12px 12px 12px 26px;
          transition:filter var(--t-fast) var(--e-sharp),
                     transform var(--t-fast) var(--e-sharp); }}
button:hover {{ filter:brightness(1.08); }}
button:active {{ transform:scale(.985); }}
button[disabled] {{ opacity:.6; cursor:progress; }}
.keep {{ margin:1.2rem 0 0; font-size:.82rem; color:var(--ink3); }}
.err {{ margin:0; font-size:.9rem; color:var(--terra); font-weight:600; }}
.hp {{ position:absolute; left:-9999px; width:1px; height:1px; overflow:hidden; }}

.done {{ background:var(--surface); border:1px solid var(--edge);
         border-radius:18px 18px 18px 38px; padding:clamp(1.4rem,4vw,2.2rem); }}
.done h2 {{ margin:0 0 .5rem; font-size:1.25rem; letter-spacing:-.015em; }}
.done p {{ margin:0; color:var(--ink2); }}

footer {{ margin-top:2.4rem; padding-top:1.2rem; border-top:1px solid var(--edge);
          display:flex; flex-wrap:wrap; gap:1.2rem; color:var(--ink3); font-size:.85rem; }}
footer a {{ color:var(--ink3); display:inline-flex; align-items:center; min-height:44px; }}
@media (prefers-reduced-motion:reduce) {{ * {{ transition:none !important; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="/static/img/icon.png" alt="" width="40" height="40">
    <a class="name" href="{back_href}">Sayr</a>
    <a class="langlink" href="{other_href}"><span>{other_label}</span></a>
  </header>

  <h1>{h1}</h1>
  <p class="lede">{lede}</p>

  <form id="report" method="post" action="/report" enctype="multipart/form-data">
    <div class="field">
      <label class="lab" for="place">{place_label}</label>
      <input type="text" id="place" name="place" list="places" maxlength="200"
             autocomplete="off" placeholder="{place_ph}" value="{place_value}">
      <p class="hint">{place_hint}</p>
    </div>
    <datalist id="places">{options}</datalist>

    <div class="field">
      <span class="lab">{topics_label}</span>
      <div class="chips">{chips}</div>
      <p class="hint">{topics_hint}</p>
    </div>

    <div class="field">
      <label class="lab" for="comment">{comment_label}</label>
      <textarea id="comment" name="comment" maxlength="2000"
                placeholder="{comment_ph}"></textarea>
    </div>

    <div class="field">
      <span class="lab">{files_label}</span>
      <label class="drop">
        <input type="file" id="files" name="files" multiple
               accept="image/jpeg,image/png,image/webp,image/heic,image/heif,.heic,.heif,application/pdf,.gpx">
        <span class="face">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path
            d="M20 11.5 12.4 19a4.6 4.6 0 0 1-6.5-6.5l7.6-7.6a3.1 3.1 0 0 1 4.4 4.4l-7.6 7.6a1.5 1.5 0 0 1-2.2-2.2l7-7"/></svg>
          <b>{files_button}</b>
          <em>{files_types}</em>
        </span>
      </label>
      <ul class="picked" data-picked hidden></ul>
      <p class="hint">{files_hint}</p>
    </div>

    <div class="field">
      <label class="lab" for="contact">{contact_label}</label>
      <input type="text" id="contact" name="contact" maxlength="120"
             autocomplete="off" placeholder="{contact_ph}">
      <p class="hint">{contact_hint}</p>
    </div>

    <div class="send">
      <button type="submit">{submit}</button>
      <p class="err" data-err hidden></p>
    </div>
    <input class="hp" type="text" name="website" tabindex="-1" autocomplete="off">
    <input type="hidden" name="lang" value="{lang}">
    <p class="keep">{keep}</p>
  </form>

  <footer>
    <a href="{back_href}">{back}</a>
    <a href="/privacy">{privacy}</a>
    <a href="mailto:{email}">{email}</a>
  </footer>
</div>
<script>
(function () {{
  var f = document.getElementById('report');
  if (!f || !window.fetch) return;
  var btn = f.querySelector('button');
  var err = f.querySelector('[data-err]');
  var label = btn.textContent;
  var files = f.elements.files;
  var list = f.querySelector('[data-picked]');
  var MAX_FILES = {max_files}, MAX_BYTES = {max_bytes}, MAX_TOTAL = {max_total};

  function size(n) {{
    return n < 1048576 ? Math.max(1, Math.round(n / 1024)) + ' KB'
                       : (n / 1048576).toFixed(1) + ' MB';
  }}

  // Выбранное показываем сразу: браузер сам пишет только «файлов: 3»,
  // и по этой строке не видно, тот ли кадр приложен
  function show() {{
    list.textContent = '';
    var chosen = files.files;
    for (var i = 0; i < chosen.length; i++) {{
      var li = document.createElement('li');
      var name = document.createElement('span');
      var bytes = document.createElement('em');
      name.textContent = chosen[i].name;
      bytes.textContent = size(chosen[i].size);
      li.appendChild(name);
      li.appendChild(bytes);
      list.appendChild(li);
    }}
    list.hidden = !chosen.length;
  }}

  // Пределы те же, что на сервере. Здесь — чтобы человек узнал о лишнем
  // файле до того, как восемь мегабайт уедут по мобильному интернету
  function heavy() {{
    var chosen = files.files, total = 0;
    if (chosen.length > MAX_FILES) return {too_many!r};
    for (var i = 0; i < chosen.length; i++) {{
      if (chosen[i].size > MAX_BYTES) {{
        return {too_big!r}.replace('{{name}}', chosen[i].name);
      }}
      total += chosen[i].size;
    }}
    return total > MAX_TOTAL ? {too_heavy!r} : '';
  }}

  if (files && list) files.addEventListener('change', function () {{
    show();
    err.hidden = true;
  }});

  f.addEventListener('submit', function (e) {{
    var comment = f.comment.value.trim();
    var picked = f.querySelectorAll('.chip input:checked').length;
    var attached = files && files.files ? files.files.length : 0;
    // Пустую заявку разворачиваем здесь же: сервер ответит то же самое,
    // но человеку не придётся ждать ответа, чтобы это узнать
    if (!comment && !picked && !attached) {{
      e.preventDefault();
      err.textContent = {empty!r};
      err.hidden = false;
      return;
    }}
    var tooMuch = attached ? heavy() : '';
    if (tooMuch) {{
      e.preventDefault();
      err.textContent = tooMuch;
      err.hidden = false;
      return;
    }}
    e.preventDefault();
    err.hidden = true;
    btn.disabled = true;
    btn.textContent = {sending!r};
    fetch(f.action, {{ method: 'POST', body: new FormData(f),
                       headers: {{ 'Accept': 'application/json' }} }})
      .then(function (r) {{
        if (r.ok) return r.json();
        // Сервер объясняет отказ словами — их и показываем: «не отправилось»
        // на пустой заявке или упёртом пределе сбивает с толку
        return r.json().then(function (j) {{ throw (j && j.detail) || 0; }},
                             function () {{ throw 0; }});
      }})
      .then(function () {{
        var done = document.createElement('div');
        done.className = 'done';
        done.innerHTML = '<h2></h2><p></p>';
        done.querySelector('h2').textContent = {ok_head!r};
        done.querySelector('p').textContent = {ok_body!r};
        f.replaceWith(done);
        done.scrollIntoView({{ block: 'center' }});
      }})
      .catch(function (why) {{
        btn.disabled = false;
        btn.textContent = label;
        err.textContent = typeof why === 'string' && why ? why : {fail!r};
        err.hidden = false;
      }});
  }});
}})();
</script>
</body>
</html>"""

_THANKS = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sayr</title>
<style>body{{margin:0;background:#F3EEE3;color:#161A17;
font-family:ui-sans-serif,system-ui,sans-serif;display:grid;place-items:center;
min-height:100vh;padding:2rem}}
@media (prefers-color-scheme:dark){{body{{background:#141714;color:#EDEAE1}}}}
main{{max-width:26rem;text-align:center}}h1{{font-size:1.4rem}}
p{{color:#57524A}}a{{color:#2F5D3F}}</style></head>
<body><main><h1>{head}</h1><p>{body}</p><p><a href="{back}">{back_label}</a></p></main>
</body></html>"""


async def _catalog(session: AsyncSession, lang: Lang) -> list[tuple[Place, str]]:
    """Опубликованные места с подписью для списка: «Название — регион».

    Подпись — единственное, что связывает выбор человека со строкой в базе:
    список отдаётся `<datalist>`, а разбирается та же подпись обратно. Имена
    в каталоге повторяются (два «Пальтау» рядом), поэтому к двойникам
    дописывается категория — иначе выбор был бы делом случая.
    """
    stmt = (
        select(Place)
        .where(Place.is_published)
        .options(selectinload(Place.region))
    )
    places = list((await session.execute(stmt)).scalars().all())
    rows: list[list] = []
    counts: dict[str, int] = {}
    for place in places:
        name = pick(place.name, place.name_uz, lang)
        region = pick(place.region.name, place.region.name_uz, lang) if place.region else ""
        label = f"{name} — {region}" if region else name
        counts[label] = counts.get(label, 0) + 1
        rows.append([place, label])
    for row in rows:
        place, label = row
        if counts[label] > 1:
            category = CATEGORY[lang].get(place.category.value, "")
            row[1] = f"{label}, {category}" if category else f"{label} ({place.slug})"
    rows.sort(key=lambda row: row[1].casefold())
    return [(place, label) for place, label in rows]


def _resolve(rows: list[tuple[Place, str]], typed: str) -> int | None:
    """Строка из поля — в место каталога.

    Ловим три способа попасть: выбранная из списка подпись, одно голое имя
    (если оно в каталоге одно) и slug — по нему приходят со страницы места.
    Не узнали — не беда, текст уедет в `place_note` как есть.
    """
    needle = typed.strip().casefold()
    if not needle:
        return None
    by_label = {label.casefold(): place.id for place, label in rows}
    if needle in by_label:
        return by_label[needle]
    by_slug = {place.slug.casefold(): place.id for place, _ in rows}
    if needle in by_slug:
        return by_slug[needle]
    names: dict[str, list[int]] = {}
    for place, label in rows:
        names.setdefault(label.split(" — ")[0].casefold(), []).append(place.id)
    hit = names.get(needle)
    return hit[0] if hit and len(hit) == 1 else None


#: Сколько заявок с одного адреса принимаем за час. Счётчик живёт в памяти
#: процесса, а воркеров два — значит настоящий предел вдвое выше. Точность
#: тут и не нужна: от настойчивого спамера это не защита, а от скрипта,
#: который дёргает форму в цикле, — вполне.
#:
#: Тридцать, а не пять: сотовые операторы держат за одним адресом пол-города,
#: и низкий порог молча съедал бы живые заявки. Упёршемуся отвечаем словами,
#: а не тишиной, — иначе человек будет думать, что письмо ушло
_LIMIT, _WINDOW = 30, 3600
_RECENT: dict[str, list[float]] = {}


def _too_often(ip: str) -> bool:
    now = time.monotonic()
    for key in [k for k, v in _RECENT.items() if all(now - t > _WINDOW for t in v)]:
        del _RECENT[key]
    fresh = [t for t in _RECENT.get(ip, []) if now - t < _WINDOW]
    _RECENT[ip] = fresh
    if len(fresh) >= _LIMIT:
        return True
    fresh.append(now)
    return False


def _render(t: dict, rows: list[tuple[Place, str]], place_value: str) -> str:
    options = "".join(f'<option value="{escape(label, quote=True)}"></option>'
                      for _, label in rows)
    chips = "".join(
        '<label class="chip"><input type="checkbox" name="topics" '
        f'value="{code}"><span>{ru if t["lang"] == "ru" else uz}</span></label>'
        for code, ru, uz in TOPICS
    )
    return _PAGE.format(
        options=options,
        chips=chips,
        max_files=attachments.MAX_FILES,
        max_bytes=attachments.MAX_BYTES,
        max_total=attachments.MAX_TOTAL,
        place_value=escape(place_value, quote=True),
        other_href=t["other"][0],
        other_label=t["other"][1],
        email=SUPPORT_EMAIL,
        privacy="Конфиденциальность" if t["lang"] == "ru" else "Maxfiylik",
        **{k: v for k, v in t.items() if k != "other"},
    )


@router.get("/report", response_class=HTMLResponse)
async def report_form(
    place: str = Query("", description="slug места: подставится в поле"),
    session: AsyncSession = Depends(get_session),
) -> str:
    return await _form("ru", place, session)


@router.get("/uz/report", response_class=HTMLResponse)
async def report_form_uz(
    place: str = Query(""),
    session: AsyncSession = Depends(get_session),
) -> str:
    return await _form("uz", place, session)


async def _form(lang: Lang, place: str, session: AsyncSession) -> str:
    rows = await _catalog(session, lang)
    prefill = ""
    if place:
        slug = place.strip().casefold()
        prefill = next((label for p, label in rows if p.slug.casefold() == slug), "")
    return _render(_T[lang], rows, prefill)


async def _attached(files: list[UploadFile], t: dict) -> list[PlaceReportFile]:
    """Присланные файлы — на диск, строками к заявке.

    Отказ объясняем словами и с именем файла: человек приложил четыре
    кадра, и «слишком тяжело» без имени не говорит, какой из них убрать.

    Общий вес считаем по ходу чтения, а не после: смысл предела в том,
    чтобы не держать в памяти чужие сто мегабайт. Настоящая же оборона
    от такого стоит раньше — `client_max_body_size` у nginx, который
    обрывает толстое тело, не доводя его до приложения.
    """
    if len(files) > attachments.MAX_FILES:
        raise HTTPException(422, t["too_many"])

    rows: list[PlaceReportFile] = []
    written: list[str] = []
    total = 0
    try:
        for upload in files:
            named = upload.filename or ""
            # Толстый файл разворачиваем, не читая: сюда он уже доехал,
            # но незачем ещё и поднимать его в память целиком
            if upload.size is not None and upload.size > attachments.MAX_BYTES:
                raise attachments.Rejected("too_big", named)
            data = await upload.read()
            total += len(data)
            if total > attachments.MAX_TOTAL:
                raise HTTPException(422, t["too_heavy"])
            name, mime, size, fresh = attachments.save(data, named)
            if fresh:
                written.append(name)
            rows.append(
                PlaceReportFile(
                    name=name,
                    original_name=named[:200],
                    content_type=mime,
                    size=size,
                )
            )
    except attachments.Rejected as no:
        _forget(written)
        # «Пустой файл» отдельного разговора не стоит: браузер присылает
        # такое, когда файл читается не до конца
        reason = "bad_type" if no.reason == "empty" else no.reason
        raise HTTPException(422, t[reason].format(name=no.filename)) from no
    except HTTPException:
        # Отказ на третьем файле не повод оставлять на диске два первых:
        # заявки не будет, а байты остались бы навсегда
        _forget(written)
        raise
    return rows


def _forget(names: list[str]) -> None:
    """Убирает то, что успели записать до отказа.

    Только записанное этой отправкой (`fresh`): имя файла — хеш его
    содержимого, и тот же кадр мог прийти раньше в чужой заявке.
    """
    for name in names:
        attachments.drop(name)


@router.post("/report")
async def report_submit(
    request: Request,
    place: str = Form(""),
    topics: list[str] = Form([]),
    comment: str = Form(""),
    contact: str = Form(""),
    lang: str = Form("ru"),
    website: str = Form(""),
    files: list[UploadFile] = File([]),
    accept: str = Header("", alias="Accept"),
    session: AsyncSession = Depends(get_session),
):
    """Приём заявки.

    Пойманный приманкой бот получает ровно тот же ответ, что и человек:
    узнав, что его раскусили, он бы просто перестал заполнять ловушку.
    А вот отказы по делу — пустая заявка, упёршийся предел, непринятый
    файл — объясняются словами: их читает человек, и молчание он примет
    за отправку.
    """
    lang = lang if lang in ("ru", "uz") else "ru"
    t = _T[lang]
    comment = comment.strip()[:2000]
    picked = [code for code in topics if code in TOPIC_CODES]
    # Пустое поле формы браузер всё равно присылает — частью без имени
    sent = [f for f in files if f is not None and f.filename]
    # Один снимок развилки — уже заявка: он объясняет больше, чем абзац
    if not comment and not picked and not sent:
        raise HTTPException(422, t["empty"])

    # Приманка отвечает боту как всем: разный ответ подсказал бы ему,
    # что поле-ловушку надо оставить пустым
    if not website:
        if _too_often(request.client.host if request.client else "?"):
            raise HTTPException(429, t["too_often"])
        rows = await _catalog(session, lang)
        typed = place.strip()[:200]
        place_id = _resolve(rows, typed)
        session.add(
            PlaceReport(
                place_id=place_id,
                # Записанное словами храним, только когда место не узнали:
                # у узнанного имя и так живёт в каталоге
                place_note=None if place_id or not typed else typed,
                topics=picked,
                comment=comment,
                contact=normalize_contact(contact)[:120] or None,
                lang=lang,
                files=await _attached(sent, t),
            )
        )
        await session.commit()

    if "application/json" in accept:
        return JSONResponse({"ok": True})
    return HTMLResponse(
        _THANKS.format(
            lang=lang, head=t["ok_head"], body=t["ok_body"],
            back=t["back_href"], back_label=t["back"],
        )
    )
