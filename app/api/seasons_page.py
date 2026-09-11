"""Разметка игры «когда сюда идти» и общий круг года.

Круг живёт здесь, а не в странице проверяющего, потому что он нужен обоим:
игрок им отвечает, проверяющий — правит перед одобрением. Виджет один,
разметка одна, поведение одно.

Шаблон подставляется через `%%имя%%`, а не через `str.format`: в CSS
и в скрипте фигурных скобок больше, чем текста, и удваивать их все
(как в `report.py`) — значит однажды пропустить одну и искать её полдня.
"""

import math
from html import escape

from ..reports import CATEGORY
from ..seasons import DEFAULT_ARC, FROM_RU, MONTHS_UZ, SHORT_RU, SHORT_UZ, arc_text

# Круг: 280 × 280, середина 140, кольцо радиусом 96, подписи по 122.
# Месяцы идут по часовой стрелке, граница декабря и января — наверху,
# июня и июля — внизу; значит весна справа, осень слева, как их и держат
# в голове (спека 2026-09-11-season-game-design.md)
_MID = 140
_RING = 96
_LABEL = 122


def _fill(template: str, **values: object) -> str:
    for key, value in values.items():
        template = template.replace(f"%%{key}%%", str(value))
    return template


def arc_path(start: int, end: int, radius: int = _RING) -> str:
    """Дуга по кольцу от начала первого месяца до конца последнего.

    Та же математика, что в скрипте круга: месяцы закрашиваются целиком,
    иначе край полосы врезался бы в середину подписи.
    """
    def edge(deg: float) -> tuple[float, float]:
        rad = math.radians(deg - 90)
        return _MID + radius * math.cos(rad), _MID + radius * math.sin(rad)

    span = (end - start) % 12 + 1
    ax, ay = edge((start - 1) * 30)
    if span == 12:
        # Замкнутое кольцо одной дугой не нарисовать: концы совпадают.
        # Две ровные половины через противоположную точку — у полуокружности
        # радиус однозначен, и полоса не вылезает за кольцо
        ox, oy = 2 * _MID - ax, 2 * _MID - ay
        return (f"M{ax:.1f} {ay:.1f}A{radius} {radius} 0 0 1 {ox:.1f} {oy:.1f}"
                f"A{radius} {radius} 0 0 1 {ax:.1f} {ay:.1f}")
    bx, by = edge(end * 30)
    big = 1 if span > 6 else 0
    return f"M{ax:.1f} {ay:.1f}A{radius} {radius} 0 {big} 1 {bx:.1f} {by:.1f}"


def circle_svg(
    short_months: tuple[str, ...],
    seasons: tuple[str, ...],
    others: tuple[tuple[int, int], ...] = (),
    band: tuple[int, int] | None = None,
) -> str:
    """Круг года: деления, подписи месяцев и слова сезонов внутри.

    Дуга и маркеры дорисовываются скриптом: до первого ответа их нет вовсе.
    Пустой круг — честное «ещё не выбрано», а поставленная по умолчанию
    дуга подсказывала бы ответ, которого человек не давал.
    """
    ticks = []
    for index in range(12):
        # Деление стоит на ГРАНИЦЕ месяцев, а не в середине: полоса дуги
        # обрывается ровно по нему
        angle = math.radians(index * 30 - 90)
        inner, outer = _RING - 13, _RING + 13
        x1, y1 = _MID + inner * math.cos(angle), _MID + inner * math.sin(angle)
        x2, y2 = _MID + outer * math.cos(angle), _MID + outer * math.sin(angle)
        ticks.append(
            f'<line class="tick" x1="{x1:.1f}" y1="{y1:.1f}" '
            f'x2="{x2:.1f}" y2="{y2:.1f}"/>'
        )
    labels = []
    for index, name in enumerate(short_months):
        angle = math.radians(index * 30 + 15 - 90)
        x = _MID + _LABEL * math.cos(angle)
        y = _MID + _LABEL * math.sin(angle)
        labels.append(
            f'<text class="mon" x="{x:.1f}" y="{y:.1f}">{escape(name)}</text>'
        )
    # Слова сезонов — бледно и внутри кольца: они не подписи к делениям,
    # а подсказка, куда вообще смотреть
    corners = [
        (seasons[3], _MID, _MID - 58),   # зима сверху
        (seasons[0], _MID + 58, _MID),   # весна справа
        (seasons[1], _MID, _MID + 62),   # лето снизу
        (seasons[2], _MID - 58, _MID),   # осень слева
    ]
    words = "".join(
        f'<text class="sea" x="{x}" y="{y}">{escape(word)}</text>'
        for word, x, y in corners
    )
    # Чужие ответы — бледными дугами по тому же кольцу, каждая на своём
    # радиусе: наложенные друг на друга, они читались бы как одна
    spread = "".join(
        f'<path class="other" d="{arc_path(start, end, _RING - 9 + index % 3 * 9)}"/>'
        for index, (start, end) in enumerate(others)
    )
    ready = band is not None
    grip_from = pos_on_ring(band[0]) if ready else (0.0, 0.0)
    grip_to = pos_on_ring(band[1]) if ready else (0.0, 0.0)
    off = "" if ready else " hidden"
    return (
        f'<svg class="dial" viewBox="0 0 280 280" data-dial>'
        f'<circle class="ring" cx="{_MID}" cy="{_MID}" r="{_RING}"/>'
        f"{spread}"
        f'<path class="band" data-band d="{arc_path(*band) if ready else ""}"/>'
        f'{"".join(ticks)}{"".join(labels)}{words}'
        f'<circle class="grip" data-grip="from" cx="{grip_from[0]:.1f}" '
        f'cy="{grip_from[1]:.1f}" r="15"{off}/>'
        f'<circle class="grip" data-grip="to" cx="{grip_to[0]:.1f}" '
        f'cy="{grip_to[1]:.1f}" r="15"{off}/>'
        f"</svg>"
    )


def pos_on_ring(month: int) -> tuple[float, float]:
    """Середина месяца на кольце — туда встаёт маркер."""
    rad = math.radians((month - 0.5) * 30 - 90)
    return _MID + _RING * math.cos(rad), _MID + _RING * math.sin(rad)


#: Общий стиль обеих страниц: токены, шапка, карточка, кнопки.
#: Игра и проверка берут его целиком — так они не разъедутся
SHARED_CSS = """
@font-face { font-family:Plex; font-weight:400; font-display:optional;
             src:url(/static/fonts/IBMPlexSans-Regular.woff2) format('woff2'); }
@font-face { font-family:Plex; font-weight:600; font-display:optional;
             src:url(/static/fonts/IBMPlexSans-SemiBold.woff2) format('woff2'); }
@font-face { font-family:PlexMono; font-weight:500; font-display:optional;
             src:url(/static/fonts/IBMPlexMono-Medium.woff2) format('woff2'); }
:root {
  --paper:#F3EEE3; --surface:#FBF8F1; --ink:#161A17; --ink2:#57524A; --ink3:#726A5C;
  --green:#2F5D3F; --terra:#C75B12; --cta:#B04E0C; --on-cta:#FFFFFF;
  --edge:#161A1714; --line:#161A1726; --shadow:#161A1722;
}
@media (prefers-color-scheme:dark) {
  :root { --paper:#141714; --surface:#1E221E; --ink:#EDEAE1; --ink2:#C6C1B5;
          --ink3:#8E897D; --green:#7FBF95; --terra:#E8843C; --cta:#E8843C;
          --on-cta:#1F0F06; --edge:#EDEAE11F; --line:#EDEAE133; --shadow:#00000055; }
}
*,*::before,*::after { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink);
       font-family:Plex,ui-sans-serif,system-ui,sans-serif; line-height:1.5;
       -webkit-font-smoothing:antialiased; }
a { color:var(--green); }
:focus-visible { outline:2px solid var(--terra); outline-offset:3px; border-radius:6px; }
.wrap { max-width:32rem; margin:0 auto; min-height:100dvh;
        padding:clamp(.9rem,3vw,1.4rem) clamp(1rem,5vw,1.6rem) 1.4rem;
        display:flex; flex-direction:column; }

header { display:flex; align-items:center; gap:.7rem; margin-bottom:.9rem; }
header img { width:32px; height:32px; border-radius:9px; }
.name { font-weight:600; font-size:1.05rem; color:var(--ink); text-decoration:none; }
.left { margin-left:auto; margin-right:.3rem; font-family:PlexMono,monospace;
        font-size:.72rem; letter-spacing:.08em; text-transform:uppercase;
        color:var(--ink3); white-space:nowrap; }
.langlink { font-family:PlexMono,monospace; font-size:.7rem; letter-spacing:.08em;
            text-transform:uppercase; color:var(--ink3); text-decoration:none; }

h1 { font-size:clamp(1.3rem,4.5vw,1.7rem); line-height:1.15; margin:0 0 .35rem;
     letter-spacing:-.02em; }
.lede { color:var(--ink2); margin:0 0 1rem; font-size:.92rem; max-width:34ch; }

/* Карточка — та же форма, что у полароидов каталога: срезанный угол
   и подпись под кадром */
.card { background:var(--surface); border:1px solid var(--edge);
        border-radius:18px 18px 18px 34px; overflow:hidden;
        box-shadow:0 14px 34px -22px var(--shadow);
        transition:transform .2s cubic-bezier(.4,0,.2,1), opacity .2s; }
.card.gone { transform:translateY(-14px) scale(.97); opacity:0; }
.shot { aspect-ratio:16/10; background:var(--line); }
.shot img { width:100%; height:100%; object-fit:cover; display:block; }
.card h2 { font-size:1.15rem; margin:.7rem .9rem .15rem; letter-spacing:-.015em; }
.meta { margin:0 .9rem .8rem; font-family:PlexMono,monospace; font-size:.72rem;
        letter-spacing:.06em; text-transform:uppercase; color:var(--ink3); }

.dialbox { display:none; margin:1rem 0 .2rem; }
.js .dialbox { display:block; }
.arc { text-align:center; font-weight:600; font-size:1rem; margin:.5rem 0 0;
       min-height:1.5rem; }
.arc.empty { color:var(--ink3); font-weight:400; font-size:.9rem; }

/* Запасной путь: без скрипта круг не нужен, месяцы выбираются списками */
.plain { display:flex; gap:.6rem; align-items:center; margin:1rem 0 .2rem;
         flex-wrap:wrap; }
.js .plain { display:none; }
.plain label { display:flex; align-items:center; gap:.4rem; color:var(--ink2);
               font-size:.9rem; }
select { font:inherit; font-size:.95rem; padding:.5rem .6rem; min-height:44px;
         border:1px solid var(--line); border-radius:11px;
         background:var(--paper); color:var(--ink); }

.send { display:flex; gap:.7rem; margin-top:auto; padding-top:1.1rem; }
button { font:inherit; font-weight:600; font-size:1rem; cursor:pointer;
         min-height:52px; flex:1; border-radius:14px 14px 14px 26px;
         border:0; transition:filter .16s, transform .16s; }
button:active { transform:scale(.985); }
.ghost { background:transparent; color:var(--ink2); border:1px solid var(--line);
         border-radius:14px; }
.go { background:var(--cta); color:var(--on-cta); }
.go[disabled] { opacity:.4; cursor:default; }

.done { background:var(--surface); border:1px solid var(--edge);
        border-radius:18px 18px 18px 34px; padding:1.4rem; margin-top:1rem; }
.done h2 { margin:0 0 .4rem; font-size:1.2rem; }
.done p { margin:0 0 .8rem; color:var(--ink2); }
"""

#: Стили круга — общие для игры и для страницы проверяющего
CIRCLE_CSS = """
.dial { width: min(72vw, 300px); height: auto; touch-action: none;
        display: block; margin: 0 auto; }
/* Круг тянут пальцем: без этого подписи месяцев выделяются синим,
   а долгое нажатие открывает меню «скопировать». Инпутов здесь нет,
   поэтому запрет безопасен — на странице входа он не стоит */
.dialbox, .dial, .card, .send, .arc, .votes {
  -webkit-user-select: none; user-select: none;
  -webkit-touch-callout: none; -webkit-tap-highlight-color: transparent; }
.dial .ring { fill: none; stroke: var(--line); stroke-width: 26; }
.dial .band { fill: none; stroke: var(--green); stroke-width: 26;
              stroke-linecap: butt; }
.dial .tick { stroke: var(--paper); stroke-width: 1.5; }
.dial .mon { font-family: PlexMono, monospace; font-size: 10px; fill: var(--ink3);
             text-anchor: middle; dominant-baseline: middle; }
.dial .sea { font-family: PlexMono, monospace; font-size: 9px; fill: var(--ink3);
             letter-spacing: .12em; text-anchor: middle; dominant-baseline: middle;
             opacity: .65; }
.dial .grip { fill: var(--surface); stroke: var(--green); stroke-width: 3;
              cursor: grab; }
/* Атрибут hidden у SVG браузеры игнорируют: без этого правила маркеры
   торчат в левом верхнем углу, где их и оставили нулевые координаты */
.dial .grip[hidden] { display: none; }
.dial .other { fill: none; stroke: var(--green); stroke-width: 26; opacity: .18; }
"""

#: Поведение круга. Вынесено строкой, потому что страница проверяющего
#: берёт тот же скрипт: разъехавшись, они начали бы считать месяцы
#: по-разному, а сверять их было бы нечем
CIRCLE_JS = """
function dial(root, onChange, start) {
  var svg = root.querySelector('[data-dial]');
  var band = svg.querySelector('[data-band]');
  var grips = { from: svg.querySelector('[data-grip=from]'),
                to: svg.querySelector('[data-grip=to]') };
  var MID = 140, R = 96, dragging = null;
  var state = start ? { from: start[0], to: start[1] } : { from: 0, to: 0 };

  function pos(month) {
    var r = ((month - 0.5) * 30 - 90) * Math.PI / 180;
    return [MID + R * Math.cos(r), MID + R * Math.sin(r)];
  }
  // Дуга закрашивается целыми месяцами: от начала первого до конца
  // последнего, иначе край полосы врезался бы в середину подписи
  function edge(deg) {
    var r = (deg - 90) * Math.PI / 180;
    return [MID + R * Math.cos(r), MID + R * Math.sin(r)];
  }
  function span(from, to) { return ((to - from + 12) % 12) + 1; }
  // Расстояние между месяцами по кругу: от декабря до января — один шаг
  function gap(a, b) { var d = Math.abs(a - b) % 12; return Math.min(d, 12 - d); }

  function draw() {
    if (!state.from) {
      band.setAttribute('d', '');
      grips.from.setAttribute('hidden', '');
      grips.to.setAttribute('hidden', '');
      return;
    }
    var n = span(state.from, state.to);
    var a = edge((state.from - 1) * 30), d;
    if (n === 12) {
      // Целый год одной дугой не нарисовать: концы совпадают. Две ровные
      // половины через противоположную точку — у полуокружности радиус
      // однозначен. Прежний вариант строил вторую дугу не на той окружности,
      // и полоса вылезала за кольцо
      var o = [2 * MID - a[0], 2 * MID - a[1]];
      d = 'M' + a[0] + ' ' + a[1] + 'A' + R + ' ' + R + ' 0 0 1 ' + o[0] + ' ' + o[1] +
          'A' + R + ' ' + R + ' 0 0 1 ' + a[0] + ' ' + a[1];
    } else {
      var b = edge(state.to * 30);
      d = 'M' + a[0] + ' ' + a[1] + 'A' + R + ' ' + R + ' 0 ' + (n > 6 ? 1 : 0) +
          ' 1 ' + b[0] + ' ' + b[1];
    }
    band.setAttribute('d', d);
    ['from', 'to'].forEach(function (key) {
      var p = pos(state[key]);
      grips[key].setAttribute('cx', p[0]);
      grips[key].setAttribute('cy', p[1]);
      // Через атрибут, а не через .hidden: у SVG-элементов такого
      // свойства нет, и присваивание молча ничего не гасит и не зажигает
      grips[key].removeAttribute('hidden');
    });
  }

  // Где палец: месяц под ним и насколько далеко он от середины круга
  function locate(event) {
    var box = svg.getBoundingClientRect();
    var x = (event.clientX - box.left) / box.width * 280 - MID;
    var y = (event.clientY - box.top) / box.height * 280 - MID;
    var deg = (Math.atan2(y, x) * 180 / Math.PI + 90 + 360) % 360;
    return { month: Math.floor(deg / 30) + 1, dist: Math.sqrt(x * x + y * y) };
  }

  function notify() { if (onChange) onChange(state.from, state.to); }

  svg.addEventListener('pointerdown', function (e) {
    var hit = locate(e);
    // Середина круга ничего не значит: угол там скачет от малейшего
    // движения, и касание у надписи «ЛЕТО» дёргало бы край дуги
    if (hit.dist < 50) return;
    e.preventDefault();
    svg.setPointerCapture(e.pointerId);
    if (!state.from) {
      state.from = state.to = hit.month;
      dragging = 'to';
    } else {
      // Двигается БЛИЖАЙШИЙ конец дуги, а не начинается выбор заново:
      // промах мимо маркера на телефоне — норма, и сбрасывать из-за него
      // уже поставленную дугу значило бы наказывать за неточный палец
      dragging = gap(hit.month, state.from) < gap(hit.month, state.to) ? 'from' : 'to';
      state[dragging] = hit.month;
    }
    draw();
    notify();
  });
  svg.addEventListener('pointermove', function (e) {
    if (!dragging) return;
    var month = locate(e).month;
    if (month === state[dragging]) return;
    state[dragging] = month;
    draw();
    notify();
  });
  function release() { dragging = null; }
  svg.addEventListener('pointerup', release);
  svg.addEventListener('pointercancel', release);

  draw();
  return {
    set: function (from, to) { state.from = from; state.to = to; draw(); },
    get: function () { return [state.from, state.to]; }
  };
}
"""

RU = {
    "lang": "ru",
    "other": ("/uz/seasons", "Oʻzbekcha"),
    "title": "Когда сюда идти — Sayr",
    "h1": "Когда сюда идти?",
    "lede": "Проведите по кругу: с какого месяца по какой в это место стоит "
            "идти. Не были — жмите «не знаю», это тоже ответ.",
    "left": "осталось",
    "skip": "Не знаю",
    "send": "Готово",
    "pick": "Отметьте месяцы на круге",
    "from": "с",
    "to": "по",
    "thanks_head": "Спасибо!",
    "thanks_body": "Вы разметили мест: {n}. Больше показывать нечего — "
                   "остальное уже разобрали или ждёт проверки.",
    "empty_head": "Пока нечего размечать",
    "empty_body": "Все места уже с сезоном или ждут проверки. Загляните позже.",
    "back": "На главную",
    "back_href": "/",
    "months": SHORT_RU,
    "seasons": ("ВЕСНА", "ЛЕТО", "ОСЕНЬ", "ЗИМА"),
    "full": ("январь", "февраль", "март", "апрель", "май", "июнь",
             "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"),
    "gen": FROM_RU,
}

UZ = {
    "lang": "uz",
    "other": ("/seasons", "Русский"),
    "title": "Bu yerga qachon borish kerak — Sayr",
    "h1": "Bu yerga qachon borish kerak?",
    "lede": "Doira boʻylab suring: bu joyga qaysi oydan qaysi oygacha borish "
            "kerak. Bormagan boʻlsangiz — «bilmayman», bu ham javob.",
    "left": "qoldi",
    "skip": "Bilmayman",
    "send": "Tayyor",
    "pick": "Doirada oylarni belgilang",
    "from": "dan",
    "to": "gacha",
    "thanks_head": "Rahmat!",
    "thanks_body": "Siz {n} ta joyni belgiladingiz. Koʻrsatadigan boshqa "
                   "joy qolmadi — qolganlari tekshiruvda.",
    "empty_head": "Hozircha belgilaydigan joy yoʻq",
    "empty_body": "Hamma joyda mavsum bor yoki tekshiruvda. Keyinroq kiring.",
    "back": "Bosh sahifaga",
    "back_href": "/uz",
    "months": SHORT_UZ,
    "seasons": ("BAHOR", "YOZ", "KUZ", "QISH"),
    "full": MONTHS_UZ,
    "gen": MONTHS_UZ,
}

_T = {"ru": RU, "uz": UZ}


_PAGE = """<!doctype html>
<html lang="%%lang%%">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>%%title%%</title>
<meta name="robots" content="noindex">
<link rel="icon" href="/static/img/icon.png">
<link rel="preload" href="/static/fonts/IBMPlexSans-Regular.woff2" as="font"
      type="font/woff2" crossorigin>
<style>
%%shared_css%%
@media (prefers-reduced-motion:reduce) { * { transition:none !important; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="/static/img/icon.png" alt="" width="32" height="32">
    <a class="name" href="%%back_href%%">Sayr</a>
    <span class="left" data-left>%%left_text%%</span>
    <a class="langlink" href="%%other_href%%">%%other_label%%</a>
  </header>

  <h1>%%h1%%</h1>
  <p class="lede">%%lede%%</p>

  <form id="game" method="post" action="/seasons/vote" %%form_hidden%%>
    <article class="card" data-card>
      <div class="shot"><img data-thumb src="%%thumb%%" alt=""></div>
      <h2 data-name>%%name%%</h2>
      <p class="meta" data-meta>%%meta%%</p>
    </article>

    <div class="dialbox">
      %%dial%%
      <p class="arc" data-arc>%%arc%%</p>
    </div>

    <div class="plain">
      <label>%%from_label%% <select name="from_month">%%options_from%%</select></label>
      <label>%%to_label%% <select name="to_month">%%options_to%%</select></label>
    </div>

    <input type="hidden" name="slug" data-slug value="%%slug%%">
    <input type="hidden" name="lang" value="%%lang%%">
    <div class="send">
      <button type="submit" class="ghost" name="skip" value="1">%%skip%%</button>
      <button type="submit" class="go" data-go>%%send%%</button>
    </div>
  </form>

  <div class="done" data-done %%done_hidden%%>
    <h2 data-done-head>%%done_head%%</h2>
    <p data-done-body>%%done_body%%</p>
    <p><a href="%%back_href%%">%%back%%</a></p>
  </div>
</div>
<script>
document.documentElement.classList.add('js');
%%circle_js%%
(function () {
  var deck = %%deck%%, left = %%left%%, mine = %%mine%%;
  var lang = "%%lang%%";
  var full = %%full%%, gen = %%gen%%;
  var form = document.getElementById('game');
  var done = document.querySelector('[data-done]');
  var counter = document.querySelector('[data-left]');
  var arc = document.querySelector('[data-arc]');
  var go = document.querySelector('[data-go]');
  var picks = form.querySelectorAll('select');
  var DEF = %%default%%;
  var knob = dial(document.querySelector('.dialbox'), function (from, to) {
    picks[0].value = from; picks[1].value = to;
    arc.textContent = words(from, to);
  }, DEF);

  function words(from, to) {
    if (from === to) return full[from - 1];
    if (lang === 'uz') return full[from - 1] + ' — ' + full[to - 1];
    return 'с ' + gen[from - 1] + ' по ' + full[to - 1];
  }

  function show(card) {
    form.querySelector('[data-slug]').value = card.slug;
    form.querySelector('[data-name]').textContent = card.name;
    form.querySelector('[data-meta]').textContent = card.meta;
    var img = form.querySelector('[data-thumb]');
    if (card.thumb) { img.src = card.thumb; img.hidden = false; }
    else { img.removeAttribute('src'); img.hidden = true; }
    // Каждая карточка начинает с одной и той же дуги: иначе ответ на
    // прошлое место подсказывал бы ответ на следующее
    knob.set(DEF[0], DEF[1]);
    picks[0].value = DEF[0]; picks[1].value = DEF[1];
    arc.textContent = words(DEF[0], DEF[1]);
    counter.textContent = %%left_word%% + ' ' + left;
  }

  function finish() {
    form.hidden = true;
    done.hidden = false;
    document.querySelector('[data-done-head]').textContent =
      mine ? %%thanks_head%% : %%empty_head%%;
    document.querySelector('[data-done-body]').textContent =
      (mine ? %%thanks_body%% : %%empty_body%%).replace('{n}', mine);
    counter.textContent = '';
  }

  function next() {
    deck.shift();
    if (deck.length <= 3) refill();
    if (!deck.length) { finish(); return; }
    var card = form.querySelector('[data-card]');
    card.classList.add('gone');
    setTimeout(function () { show(deck[0]); card.classList.remove('gone'); }, 190);
  }

  function refill() {
    fetch('/seasons/deck?lang=' + lang, { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        left = data.left;
        // Приехали те же карточки, что уже в колоде, — берём только новые
        var have = {};
        deck.forEach(function (c) { have[c.slug] = 1; });
        data.places.forEach(function (c) { if (!have[c.slug]) deck.push(c); });
        if (!deck.length) finish();
      })
      .catch(function () {});
  }

  form.addEventListener('submit', function (e) {
    if (!window.fetch) return;
    e.preventDefault();
    var skipped = e.submitter && e.submitter.name === 'skip';
    var body = new FormData();
    body.append('slug', form.querySelector('[data-slug]').value);
    body.append('lang', lang);
    if (skipped) body.append('skip', '1');
    else { body.append('from_month', picks[0].value); body.append('to_month', picks[1].value); }
    fetch(form.action, { method: 'POST', body: body,
                         headers: { 'Accept': 'application/json' } })
      .catch(function () {});
    if (!skipped) mine += 1;
    left = Math.max(0, left - 1);
    next();
  });
})();
</script>
</body>
</html>"""


def _meta(card: dict) -> str:
    """Строка под названием: регион, категория и цифры выхода."""
    bits = [card["region"], card["category"]]
    if card.get("km"):
        bits.append(f"{card['km']:g} км")
    if card.get("hours"):
        bits.append(f"{card['hours']:g} ч")
    return " · ".join(bit for bit in bits if bit)


def render_page(lang: str, deck: list[dict], left: int, mine: int) -> str:
    """Страница игры: первая карточка разметкой, остальные — колодой в скрипте."""
    import json

    t = _T[lang if lang in _T else "ru"]
    cards = [dict(card, meta=_meta(card)) for card in deck]
    first = cards[0] if cards else None
    def options(selected: int) -> str:
        return "".join(
            f'<option value="{index + 1}"'
            f'{" selected" if index + 1 == selected else ""}>{escape(name)}</option>'
            for index, name in enumerate(t["full"])
        )
    done_head = t["thanks_head"] if mine else t["empty_head"]
    done_body = (t["thanks_body"] if mine else t["empty_body"]).replace(
        "{n}", str(mine)
    )
    return _fill(
        _PAGE,
        lang=t["lang"],
        title=escape(t["title"]),
        h1=escape(t["h1"]),
        lede=escape(t["lede"]),
        other_href=t["other"][0],
        other_label=escape(t["other"][1]),
        back=escape(t["back"]),
        back_href=t["back_href"],
        left_text=f"{t['left']} {left}" if first else "",
        skip=escape(t["skip"]),
        send=escape(t["send"]),
        from_label=escape(t["from"]),
        to_label=escape(t["to"]),
        options_from=options(DEFAULT_ARC[0]),
        options_to=options(DEFAULT_ARC[1]),
        dial=circle_svg(t["months"], t["seasons"], band=DEFAULT_ARC),
        arc=escape(arc_text(*DEFAULT_ARC, lang=t["lang"])),
        default=json.dumps(list(DEFAULT_ARC)),
        shared_css=SHARED_CSS + CIRCLE_CSS,
        circle_js=CIRCLE_JS,
        slug=escape(first["slug"], quote=True) if first else "",
        name=escape(first["name"]) if first else "",
        meta=escape(first["meta"]) if first else "",
        thumb=escape(first["thumb"] or "", quote=True) if first else "",
        form_hidden="" if first else "hidden",
        done_hidden="" if not first else "hidden",
        done_head=escape(done_head),
        done_body=escape(done_body),
        deck=json.dumps(cards, ensure_ascii=False),
        left=left,
        mine=mine,
        full=json.dumps(list(t["full"]), ensure_ascii=False),
        gen=json.dumps(list(t["gen"]), ensure_ascii=False),
        left_word=json.dumps(t["left"], ensure_ascii=False),
        thanks_head=json.dumps(t["thanks_head"], ensure_ascii=False),
        thanks_body=json.dumps(t["thanks_body"], ensure_ascii=False),
        empty_head=json.dumps(t["empty_head"], ensure_ascii=False),
        empty_body=json.dumps(t["empty_body"], ensure_ascii=False),
    )


# --- Страница проверяющего ------------------------------------------------
#
# Только по-русски: игра двуязычна, потому что в неё играют посторонние,
# а проверку делает владелец или кто-то из клуба. Второй язык здесь — это
# вдвое больше строк ради одного человека.

_LOGIN = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Проверка сезонов — Sayr</title>
<meta name="robots" content="noindex">
<link rel="icon" href="/static/img/icon.png">
<style>
%%shared_css%%
.gate { background:var(--surface); border:1px solid var(--edge);
        border-radius:18px 18px 18px 34px; padding:1.4rem; margin-top:2rem; }
.gate h1 { font-size:1.25rem; margin:0 0 .3rem; }
.gate p { color:var(--ink2); margin:0 0 1rem; font-size:.92rem; }
input[type=password] { width:100%; font:inherit; font-size:1rem; padding:.75rem .9rem;
        border:1px solid var(--line); border-radius:12px; background:var(--paper);
        color:var(--ink); min-height:48px; margin-bottom:.8rem; }
.err { color:var(--terra); font-weight:600; font-size:.9rem; margin:0 0 .8rem; }
</style>
</head>
<body>
<div class="wrap">
  <div class="gate">
    <h1>Проверка сезонов</h1>
    <p>Страница для того, кто разбирает ответы игроков.</p>
    %%error%%
    <form method="post" action="/seasons/review/login">
      <input type="password" name="password" placeholder="Пароль" autofocus
             autocomplete="current-password">
      <div class="send"><button type="submit" class="go">Войти</button></div>
    </form>
  </div>
</div>
</body>
</html>"""


_REVIEW = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Проверка сезонов — Sayr</title>
<meta name="robots" content="noindex">
<link rel="icon" href="/static/img/icon.png">
<style>
%%shared_css%%
.votes { margin:.6rem 0 0; text-align:center; font-family:PlexMono,monospace;
         font-size:.74rem; letter-spacing:.04em; color:var(--ink3); }
.ready { color:var(--green); font-weight:600; }
@media (prefers-reduced-motion:reduce) { * { transition:none !important; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="/static/img/icon.png" alt="" width="32" height="32">
    <a class="name" href="/seasons/review">Sayr</a>
    <span class="left">%%waiting%%</span>
  </header>

  <h1>Что выбрали игроки</h1>
  <p class="lede">Круг показывает все ответы; жирная дуга — то, на чём сошлись.
  Подкрутите маркеры, если знаете лучше, и одобрите — сезон встанет месту.</p>

  <form method="post" action="/seasons/review/approve" %%form_hidden%%>
    <article class="card">
      <div class="shot"><img src="%%thumb%%" alt=""></div>
      <h2>%%name%%</h2>
      <p class="meta">%%meta%%</p>
    </article>

    <div class="dialbox">
      %%dial%%
      <p class="arc %%arc_class%%" data-arc>%%arc%%</p>
    </div>
    <p class="votes">%%votes%%</p>

    <div class="plain">
      <label>с <select name="from_month">%%options_from%%</select></label>
      <label>по <select name="to_month">%%options_to%%</select></label>
    </div>

    <input type="hidden" name="slug" value="%%slug%%">
    <div class="send">
      <button type="submit" class="ghost" formaction="/seasons/review/clear"
              formnovalidate>Очистить ответы</button>
      <button type="submit" class="go" data-go %%go_off%%>Одобрить</button>
    </div>
  </form>

  <div class="done" %%done_hidden%%>
    <h2>Пока нечего проверять</h2>
    <p>Как только игроки ответят, места появятся здесь.</p>
    <p><a href="/seasons">Открыть игру</a></p>
  </div>
</div>
<script>
document.documentElement.classList.add('js');
%%circle_js%%
(function () {
  var form = document.querySelector('form');
  if (!form || form.hidden) return;
  var full = %%full%%, gen = %%gen%%;
  var arc = document.querySelector('[data-arc]');
  var go = document.querySelector('[data-go]');
  var picks = form.querySelectorAll('select');
  function words(from, to) {
    if (from === to) return full[from - 1];
    return 'с ' + gen[from - 1] + ' по ' + full[to - 1];
  }
  var knob = dial(document.querySelector('.dialbox'), function (from, to) {
    picks[0].value = from; picks[1].value = to;
    arc.textContent = words(from, to);
    arc.classList.remove('empty');
    go.disabled = false;
  });
  var start = %%band%%;
  if (start) knob.set(start[0], start[1]);
})();
</script>
</body>
</html>"""


def render_login(failed: bool = False) -> str:
    return _fill(
        _LOGIN,
        shared_css=SHARED_CSS,
        error='<p class="err">Пароль не подошёл</p>' if failed else "",
    )


def render_review(place, votes: list[tuple[int, int]], waiting: int, enough: int) -> str:
    """Карточка проверки: место, все ответы и предложение."""
    import json

    from ..seasons import arc_text, suggest

    t = RU
    band = suggest(votes) if votes else None
    if place is None:
        meta = name = thumb = slug = ""
        arc = ""
        votes_line = ""
    else:
        cover = place.photos[0] if place.photos else None
        thumb = (cover.thumb_url or cover.url or "") if cover else ""
        name = place.name
        region = place.region.name if place.region else ""
        # Категория словом, а не кодом: «DESERT» в карточке — это утечка
        # базы наружу, а не подпись
        kind = CATEGORY["ru"].get(place.category.value, "")
        meta = " · ".join(bit for bit in (region, kind) if bit)
        slug = place.slug
        arc = arc_text(*band) if band else "Ответы вразнобой — поставьте дугу сами"
        spread = " · ".join(arc_text(start, end) for start, end in votes)
        mark = " · порог набран" if len(votes) >= enough else ""
        votes_line = f"{len(votes)} отв.{mark}: {spread}"

    def options(selected: int | None) -> str:
        return "".join(
            f'<option value="{index + 1}"'
            f'{" selected" if selected == index + 1 else ""}>{escape(month)}</option>'
            for index, month in enumerate(t["full"])
        )

    return _fill(
        _REVIEW,
        shared_css=SHARED_CSS + CIRCLE_CSS,
        circle_js=CIRCLE_JS,
        waiting=f"ждут проверки: {waiting}" if waiting else "",
        dial=circle_svg(t["months"], t["seasons"], tuple(votes), band),
        arc=escape(arc),
        arc_class="" if band else "empty",
        votes=escape(votes_line),
        options_from=options(band[0] if band else None),
        options_to=options(band[1] if band else None),
        slug=escape(slug, quote=True),
        name=escape(name),
        meta=escape(meta),
        thumb=escape(thumb, quote=True),
        form_hidden="" if place else "hidden",
        done_hidden="hidden" if place else "",
        go_off="" if band else "disabled",
        band=json.dumps(list(band) if band else None),
        full=json.dumps(list(t["full"]), ensure_ascii=False),
        gen=json.dumps(list(t["gen"]), ensure_ascii=False),
    )
