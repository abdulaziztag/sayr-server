"""Разметка игры «когда сюда идти» и общий круг года.

Круг живёт здесь, а не в странице проверяющего, потому что он нужен обоим:
игрок им отвечает, проверяющий — правит перед одобрением. Виджет один,
разметка одна, поведение одно.

Шаблон подставляется через `%%имя%%`, а не через `str.format`: в CSS
и в скрипте фигурных скобок больше, чем текста, и удваивать их все
(как в `report.py`) — значит однажды пропустить одну и искать её полдня.
"""

import json
import math
from html import escape

from ..reports import CATEGORY
from ..seasons import (DEFAULT_ARC, FROM_RU, FULL_RU_UP, FULL_UZ_UP, MONTHS_UZ,
                       SHORT_RU, SHORT_UZ, arc_text, centre_lines)

# Круг: 300 × 300, середина 150. Дорожка радиусом 104 и толщиной 24,
# подписи месяцев — внутри, по 76; слова сезонов — снаружи, по 138.
#
# Январь стоит ровно наверху, и месяцы идут ПРОТИВ часовой: весна по левой
# стороне, лето внизу, осень справа. Так год и лежит в голове — линия
# слева направо, загнутая концами вверх. По часовой, как на часах, весна
# оказывалась справа, и круг читался задом наперёд.
#
# Январь — серединой, а не границей декабря: тогда каждый сезон стоит
# ровно на своей стороне (зима сверху, апрель слева, июль внизу, октябрь
# справа), а не съезжает на полмесяца вбок
_MID = 150
_RING = 104
_WIDTH = 24
_LABELS = 76
_SEASONS = 138
#: Половина толщины дуги в градусах. На столько концы дуги отступают
#: внутрь от границы месяца: закруглённый край ложится ровно на границу,
#: а не залезает в соседний месяц
_CAP = math.degrees((_WIDTH / 2) / _RING)


def _fill(template: str, **values: object) -> str:
    for key, value in values.items():
        template = template.replace(f"%%{key}%%", str(value))
    return template


def _pt(deg: float, radius: float = _RING) -> tuple[float, float]:
    """Точка на круге. Угол — по часовой от верха."""
    rad = math.radians(deg)
    return _MID + radius * math.sin(rad), _MID - radius * math.cos(rad)


def _xy(point: tuple[float, float]) -> str:
    return f"{point[0]:.1f} {point[1]:.1f}"


def _centre(month: int) -> float:
    """Середина месяца: январь наверху, дальше против часовой."""
    return -(month - 1) * 30


def _span(start: int, end: int) -> int:
    return (end - start) % 12 + 1


def _inside(month: int, start: int, end: int) -> bool:
    return (month - start) % 12 < _span(start, end)


def arc_ends(start: int, end: int) -> tuple[tuple[float, float], tuple[float, float]]:
    """Концы толстой дуги — туда же встают ручки."""
    return _pt(_centre(start) + 15 - _CAP), _pt(_centre(end) - 15 + _CAP)


def band_path(start: int, end: int) -> str:
    """Толстая дуга выбора: скруглённые края ровно по границам месяцев.

    Круглый год — две полуокружности через противоположную точку: одной
    дугой замкнутое кольцо не нарисовать, концы совпадают, а у ровной
    половины радиус однозначен и полоса не вылезает за кольцо.
    """
    r = _RING
    if _span(start, end) == 12:
        top, bottom = _pt(0), _pt(180)
        return f"M{_xy(top)}A{r} {r} 0 0 0 {_xy(bottom)}A{r} {r} 0 0 0 {_xy(top)}"
    a, z = arc_ends(start, end)
    big = 1 if _span(start, end) * 30 - 2 * _CAP > 180 else 0
    return f"M{_xy(a)}A{r} {r} 0 {big} 0 {_xy(z)}"


def ring_path(start: int, end: int, radius: float) -> str:
    """Тонкая дуга чужого ответа — строго по границам месяцев."""
    if _span(start, end) == 12:
        top, bottom = _pt(0, radius), _pt(180, radius)
        return (f"M{_xy(top)}A{radius} {radius} 0 0 0 {_xy(bottom)}"
                f"A{radius} {radius} 0 0 0 {_xy(top)}")
    a = _pt(_centre(start) + 15, radius)
    z = _pt(_centre(end) - 15, radius)
    big = 1 if _span(start, end) * 30 > 180 else 0
    return f"M{_xy(a)}A{radius} {radius} 0 {big} 0 {_xy(z)}"


def circle_svg(
    short_months: tuple[str, ...],
    seasons: tuple[str, ...],
    others: tuple[tuple[int, int], ...] = (),
    band: tuple[int, int] | None = None,
    centre: tuple[str, str] = ("", ""),
) -> str:
    """Круг года целиком, уже в нужном состоянии — скрипт его только двигает.

    Слова сезонов стоят снаружи, но только там, где нет чужих ответов:
    у проверяющего по внешнему краю идут тонкие кольца ответов, и слова
    легли бы на них.
    """
    parts = ['<svg class="dial" viewBox="0 0 300 300" data-dial>']
    parts.append(f'<circle class="track" cx="{_MID}" cy="{_MID}" r="{_RING}"/>')
    # Ответы игроков — тонкими кольцами снаружи, каждое на своём радиусе:
    # наложенные друг на друга, они читались бы как одно
    for index, (start, end) in enumerate(others):
        radius = 124 + index % 4 * 5
        parts.append(f'<path class="other" d="{ring_path(start, end, radius)}"/>')
    parts.append(f'<path class="band" data-band d="{band_path(*band) if band else ""}"/>')
    for index, name in enumerate(short_months):
        month = index + 1
        x, y = _pt(_centre(month), _LABELS)
        lit = " on" if band and _inside(month, *band) else ""
        parts.append(
            f'<text class="mon{lit}" data-mon="{month}" x="{x:.1f}" y="{y:.1f}">'
            f"{escape(name)}</text>"
        )
    if not others:
        spring, summer, autumn, winter = seasons
        for word, deg, turn in ((winter, 0, 0), (spring, -90, -90),
                                (summer, 180, 0), (autumn, 90, 90)):
            x, y = _pt(deg, _SEASONS)
            parts.append(
                f'<text class="sea" x="{x:.1f}" y="{y:.1f}" '
                f'transform="rotate({turn} {x:.1f} {y:.1f})">{escape(word)}</text>'
            )
    parts.append(f'<text class="c1" data-c1 x="{_MID}" y="{_MID - 8}">{escape(centre[0])}</text>')
    parts.append(f'<text class="c2" data-c2 x="{_MID}" y="{_MID + 15}">{escape(centre[1])}</text>')
    ends = arc_ends(*band) if band else ((0.0, 0.0), (0.0, 0.0))
    off = "" if band else " hidden"
    for key, point in zip(("from", "to"), ends):
        parts.append(
            f'<circle class="knob" data-knob="{key}" cx="{point[0]:.1f}" '
            f'cy="{point[1]:.1f}" r="13"{off}/>'
        )
        parts.append(
            f'<circle class="pip" data-pip="{key}" cx="{point[0]:.1f}" '
            f'cy="{point[1]:.1f}" r="4"{off}/>'
        )
    parts.append("</svg>")
    return "".join(parts)


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
  --edge:#161A1714; --line:#161A171F; --shadow:#161A1722;
}
@media (prefers-color-scheme:dark) {
  :root { --paper:#141714; --surface:#1E221E; --ink:#EDEAE1; --ink2:#C6C1B5;
          --ink3:#8E897D; --green:#7FBF95; --terra:#E8843C; --cta:#E8843C;
          --on-cta:#1F0F06; --edge:#EDEAE11F; --line:#EDEAE11F; --shadow:#00000055; }
}
*,*::before,*::after { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink);
       font-family:Plex,ui-sans-serif,system-ui,sans-serif; line-height:1.45;
       -webkit-font-smoothing:antialiased; }
a { color:var(--green); }
:focus-visible { outline:2px solid var(--terra); outline-offset:3px; border-radius:6px; }

/* Один экран, без прокрутки: карточка, круг и кнопки делят высоту,
   и круг забирает всё, что осталось. Ниже 560 точек — прокрутка:
   иначе круг сжался бы до марки */
.wrap { max-width:30rem; margin:0 auto; height:100dvh; min-height:560px;
        padding:max(.75rem, env(safe-area-inset-top)) 1rem
                max(.85rem, env(safe-area-inset-bottom));
        display:flex; flex-direction:column; gap:.75rem; }
header { display:flex; align-items:center; gap:.6rem; flex:none; }
header img { width:28px; height:28px; border-radius:8px; }
.name { font-weight:600; font-size:1rem; color:var(--ink); text-decoration:none; }
.left { margin-left:auto; margin-right:.3rem; font-family:PlexMono,monospace;
        font-size:.7rem; letter-spacing:.08em; text-transform:uppercase;
        color:var(--ink3); white-space:nowrap; }
.langlink { font-family:PlexMono,monospace; font-size:.68rem; letter-spacing:.08em;
            text-transform:uppercase; color:var(--ink3); text-decoration:none; }
.intro { flex:none; }
h1 { font-size:1.35rem; line-height:1.15; margin:0; letter-spacing:-.02em; }
.lede { color:var(--ink2); margin:.25rem 0 0; font-size:.86rem; }

.play { flex:1; min-height:0; display:flex; flex-direction:column; gap:.75rem; }

/* Карточка — полоса фото с подписью поверх: место узнаётся по кадру,
   а высоты уходит вдвое меньше, чем у кадра с подписью под ним */
.card { position:relative; flex:none; height:clamp(104px, 18dvh, 168px);
        border-radius:18px 18px 18px 34px; overflow:hidden; background:var(--line);
        transition:transform .2s cubic-bezier(.4,0,.2,1), opacity .2s; }
.card.gone { transform:translateX(-18px) rotate(-1.5deg); opacity:0; }
.card img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover;
            display:block; }
.cap { position:absolute; left:0; right:0; bottom:0; padding:1.9rem .9rem .65rem;
       background:linear-gradient(to bottom, rgba(12,14,12,0), rgba(12,14,12,.78)); }
.cap h2 { margin:0; font-size:1.08rem; line-height:1.2; letter-spacing:-.01em;
          color:#fff; }
.cap .meta { margin:.15rem 0 0; font-family:PlexMono,monospace; font-size:.66rem;
             letter-spacing:.06em; text-transform:uppercase; color:rgba(255,255,255,.84); }
.card.bare img { display:none; }
.card.bare .cap { background:none; }
.card.bare h2 { color:var(--ink); }
.card.bare .meta { color:var(--ink3); }

.stage { flex:1; min-height:0; display:flex; flex-direction:column;
         align-items:center; justify-content:center; }
.dialbox { display:none; height:100%; max-height:390px; aspect-ratio:1/1;
           max-width:100%; }
.js .dialbox { display:block; }

/* Запасной путь: без скрипта круг не нужен, месяцы выбираются списками */
.plain { display:flex; gap:.6rem; align-items:center; justify-content:center;
         flex-wrap:wrap; }
.js .plain { display:none; }
.plain label { display:flex; align-items:center; gap:.4rem; color:var(--ink2);
               font-size:.9rem; }
select { font:inherit; font-size:.95rem; padding:.5rem .6rem; min-height:44px;
         border:1px solid var(--line); border-radius:11px;
         background:var(--paper); color:var(--ink); }

.send { display:flex; gap:.7rem; flex:none; }
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
@media (prefers-reduced-motion:reduce) { * { transition:none !important; } }
"""

#: Стили круга. Образ — будильник «Режим сна» на айфоне: тонкая бледная
#: дорожка, поверх — толстая дуга со скруглёнными краями, на концах белые
#: ручки. Форма знакомая: людям не надо объяснять, что край тянется
CIRCLE_CSS = """
.dial { width:100%; height:100%; display:block; overflow:visible; touch-action:none; }
/* Круг тянут пальцем: без этого подписи месяцев выделяются синим,
   а долгое нажатие открывает меню «скопировать». Инпутов здесь нет,
   поэтому запрет безопасен — на странице входа он не стоит */
.dialbox, .dial, .card, .send, .votes {
  -webkit-user-select:none; user-select:none;
  -webkit-touch-callout:none; -webkit-tap-highlight-color:transparent; }
.dial .track { fill:none; stroke:var(--line); stroke-width:24; }
.dial .band { fill:none; stroke:var(--green); stroke-width:24; stroke-linecap:round; }
.dial .mon { font-family:PlexMono,monospace; font-weight:500; font-size:10.5px;
             fill:var(--ink3); text-anchor:middle; dominant-baseline:central;
             letter-spacing:.04em; transition:fill .15s; }
.dial .mon.on { fill:var(--ink); }
.dial .sea { font-family:PlexMono,monospace; font-weight:500; font-size:8.5px;
             fill:var(--ink3); letter-spacing:.24em; text-anchor:middle;
             dominant-baseline:central; opacity:.75; }
.dial .c1 { font-family:Plex,sans-serif; font-weight:600; font-size:19px;
            fill:var(--ink); text-anchor:middle; dominant-baseline:central; }
.dial .c2 { font-family:PlexMono,monospace; font-weight:500; font-size:9.5px;
            fill:var(--ink3); text-anchor:middle; dominant-baseline:central;
            letter-spacing:.1em; }
.dial .knob { fill:#fff; filter:drop-shadow(0 1px 2.5px rgba(0,0,0,.38)); cursor:grab; }
.dial .pip { fill:var(--green); pointer-events:none; }
/* Атрибут hidden у SVG браузеры игнорируют: без этого правила ручки
   торчали бы в углу, где их оставили нулевые координаты */
.dial [hidden] { display:none; }
.dial .other { fill:none; stroke:var(--green); stroke-width:3; stroke-linecap:round;
               opacity:.55; }
"""

#: Поведение круга. Одно на две страницы: разъехавшись, игра и проверка
#: начали бы считать месяцы по-разному, а сверять их было бы нечем.
#: Геометрия — та же, что в `circle_svg` выше, числа обязаны совпадать
CIRCLE_JS = r"""
function dial(root, onChange, start) {
  var svg = root.querySelector('[data-dial]');
  var band = svg.querySelector('[data-band]');
  var knobs = { from: svg.querySelector('[data-knob=from]'),
                to: svg.querySelector('[data-knob=to]') };
  var pips = { from: svg.querySelector('[data-pip=from]'),
               to: svg.querySelector('[data-pip=to]') };
  var labels = svg.querySelectorAll('[data-mon]');
  var MID = 150, R = 104, CAP = 12 / 104 * 180 / Math.PI, dragging = null;
  var state = start ? { from: start[0], to: start[1] } : { from: 0, to: 0 };

  // Угол — по часовой от верха. Январь наверху, месяцы идут против часовой
  function centre(m) { return -(m - 1) * 30; }
  function pt(deg) {
    var r = deg * Math.PI / 180;
    return (MID + R * Math.sin(r)).toFixed(1) + ' ' + (MID - R * Math.cos(r)).toFixed(1);
  }
  function span(a, b) { return ((b - a + 12) % 12) + 1; }
  // Расстояние между месяцами по кругу: от декабря до января — один шаг
  function gap(a, b) { var d = Math.abs(a - b) % 12; return Math.min(d, 12 - d); }
  function inside(m) { return ((m - state.from + 12) % 12) < span(state.from, state.to); }

  function draw() {
    var on = !!state.from;
    ['from', 'to'].forEach(function (key) {
      if (on) { knobs[key].removeAttribute('hidden'); pips[key].removeAttribute('hidden'); }
      else { knobs[key].setAttribute('hidden', ''); pips[key].setAttribute('hidden', ''); }
    });
    labels.forEach(function (el) {
      el.classList.toggle('on', on && inside(+el.getAttribute('data-mon')));
    });
    if (!on) { band.setAttribute('d', ''); return; }
    var n = span(state.from, state.to);
    var a = pt(centre(state.from) + 15 - CAP), z = pt(centre(state.to) - 15 + CAP);
    if (n === 12) {
      // Круглый год — две ровные половины: одной дугой кольцо не замкнуть
      band.setAttribute('d', 'M' + pt(0) + 'A' + R + ' ' + R + ' 0 0 0 ' + pt(180) +
                             'A' + R + ' ' + R + ' 0 0 0 ' + pt(0));
    } else {
      var big = n * 30 - 2 * CAP > 180 ? 1 : 0;
      band.setAttribute('d', 'M' + a + 'A' + R + ' ' + R + ' 0 ' + big + ' 0 ' + z);
    }
    [['from', a], ['to', z]].forEach(function (pair) {
      var xy = pair[1].split(' ');
      [knobs[pair[0]], pips[pair[0]]].forEach(function (el) {
        el.setAttribute('cx', xy[0]); el.setAttribute('cy', xy[1]);
      });
    });
  }

  // Где палец: месяц под ним и насколько далеко он от середины круга
  function locate(event) {
    var box = svg.getBoundingClientRect();
    var size = Math.min(box.width, box.height);
    var x = (event.clientX - box.left - box.width / 2) / size * 300;
    var y = (event.clientY - box.top - box.height / 2) / size * 300;
    var cw = Math.atan2(x, -y) * 180 / Math.PI;
    var ccw = ((-cw) % 360 + 360) % 360;
    return { month: Math.floor((ccw + 15) / 30) % 12 + 1,
             dist: Math.sqrt(x * x + y * y) };
  }

  function notify() { if (onChange) onChange(state.from, state.to); }

  svg.addEventListener('pointerdown', function (e) {
    var hit = locate(e);
    // Середина круга — это подпись итога, а не место для касания:
    // угол там скачет от малейшего движения, и край дуги дёргался бы
    if (hit.dist < 60) return;
    e.preventDefault();
    svg.setPointerCapture(e.pointerId);
    if (!state.from) {
      state.from = state.to = hit.month;
      dragging = 'to';
    } else {
      // Двигается БЛИЖАЙШИЙ край дуги, а не начинается выбор заново:
      // промах мимо ручки на телефоне — норма, и сбрасывать из-за него
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

// Итог в середине круга: «АПР — АВГ» крупно, длина мелко под ним
function centreText(svg, from, to, t) {
  var c1 = svg.querySelector('[data-c1]'), c2 = svg.querySelector('[data-c2]');
  var n = ((to - from + 12) % 12) + 1;
  if (n === 12) { c1.textContent = t.all; c2.textContent = ''; return; }
  c1.textContent = from === to ? t.full[from - 1] : t.short[from - 1] + ' — ' + t.short[to - 1];
  c2.textContent = t.uz ? n + ' oy' : n + ' ' + (
    (n % 10 === 1 && n !== 11) ? 'месяц' :
    (n % 10 >= 2 && n % 10 <= 4 && (n < 12 || n > 14)) ? 'месяца' : 'месяцев');
}
"""

RU = {
    "lang": "ru",
    "other": ("/uz/seasons", "Oʻzbekcha"),
    "title": "Когда сюда идти — Sayr",
    "h1": "Когда сюда идти?",
    "lede": "Подвиньте края дуги. Не были — жмите «не знаю».",
    "left": "осталось",
    "skip": "Не знаю",
    "send": "Готово",
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
    "full_up": FULL_RU_UP,
    "all": "ВЕСЬ ГОД",
    "gen": FROM_RU,
}

UZ = {
    "lang": "uz",
    "other": ("/seasons", "Русский"),
    "title": "Bu yerga qachon borish kerak — Sayr",
    "h1": "Bu yerga qachon borish kerak?",
    "lede": "Yoy chetlarini suring. Bormagan boʻlsangiz — «bilmayman».",
    "left": "qoldi",
    "skip": "Bilmayman",
    "send": "Tayyor",
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
    "full_up": FULL_UZ_UP,
    "all": "YIL BOʻYI",
    "gen": MONTHS_UZ,
}

_T = {"ru": RU, "uz": UZ}


def _centre_words(t: dict) -> str:
    """Словарь для `centreText` в скрипте."""
    return json.dumps(
        {"short": list(t["months"]), "full": list(t["full_up"]), "all": t["all"],
         "uz": t["lang"] == "uz"},
        ensure_ascii=False,
    )


def _options(t: dict, selected: int | None) -> str:
    return "".join(
        f'<option value="{index + 1}"'
        f'{" selected" if index + 1 == selected else ""}>{escape(name)}</option>'
        for index, name in enumerate(t["full"])
    )


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
%%css%%
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="/static/img/icon.png" alt="" width="28" height="28">
    <a class="name" href="%%back_href%%">Sayr</a>
    <span class="left" data-left>%%left_text%%</span>
    <a class="langlink" href="%%other_href%%">%%other_label%%</a>
  </header>

  <div class="intro">
    <h1>%%h1%%</h1>
    <p class="lede">%%lede%%</p>
  </div>

  <form id="game" class="play" method="post" action="/seasons/vote" %%form_hidden%%>
    <article class="card%%bare%%" data-card>
      <img data-thumb src="%%thumb%%" alt="">
      <div class="cap">
        <h2 data-name>%%name%%</h2>
        <p class="meta" data-meta>%%meta%%</p>
      </div>
    </article>

    <div class="stage">
      <div class="dialbox">%%dial%%</div>
      <div class="plain">
        <label>%%from_label%% <select name="from_month">%%options_from%%</select></label>
        <label>%%to_label%% <select name="to_month">%%options_to%%</select></label>
      </div>
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
  var form = document.getElementById('game');
  if (!form || form.hidden) return;
  var deck = %%deck%%, left = %%left%%, mine = %%mine%%;
  var lang = "%%lang%%", DEF = %%default%%, WORDS = %%words%%;
  var done = document.querySelector('[data-done]');
  var counter = document.querySelector('[data-left]');
  var card = form.querySelector('[data-card]');
  var picks = form.querySelectorAll('select');
  var svg = document.querySelector('[data-dial]');
  var knob = dial(document.querySelector('.dialbox'), function (from, to) {
    picks[0].value = from; picks[1].value = to;
    centreText(svg, from, to, WORDS);
  }, DEF);

  function show(next) {
    form.querySelector('[data-slug]').value = next.slug;
    form.querySelector('[data-name]').textContent = next.name;
    form.querySelector('[data-meta]').textContent = next.meta;
    var img = form.querySelector('[data-thumb]');
    if (next.thumb) { img.src = next.thumb; card.classList.remove('bare'); }
    else { img.removeAttribute('src'); card.classList.add('bare'); }
    // Каждая карточка начинает с одной и той же дуги: иначе ответ на
    // прошлое место подсказывал бы ответ на следующее
    knob.set(DEF[0], DEF[1]);
    picks[0].value = DEF[0]; picks[1].value = DEF[1];
    centreText(svg, DEF[0], DEF[1], WORDS);
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
    t = _T[lang if lang in _T else "ru"]
    cards = [dict(card, meta=_meta(card)) for card in deck]
    first = cards[0] if cards else None
    done_head = t["thanks_head"] if mine else t["empty_head"]
    done_body = (t["thanks_body"] if mine else t["empty_body"]).replace("{n}", str(mine))
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
        options_from=_options(t, DEFAULT_ARC[0]),
        options_to=_options(t, DEFAULT_ARC[1]),
        dial=circle_svg(t["months"], t["seasons"], band=DEFAULT_ARC,
                        centre=centre_lines(*DEFAULT_ARC, lang=t["lang"])),
        css=SHARED_CSS + CIRCLE_CSS,
        circle_js=CIRCLE_JS,
        slug=escape(first["slug"], quote=True) if first else "",
        name=escape(first["name"]) if first else "",
        meta=escape(first["meta"]) if first else "",
        thumb=escape(first["thumb"] or "", quote=True) if first else "",
        bare="" if first and first["thumb"] else " bare",
        form_hidden="" if first else "hidden",
        done_hidden="hidden" if first else "",
        done_head=escape(done_head),
        done_body=escape(done_body),
        deck=json.dumps(cards, ensure_ascii=False),
        left=left,
        mine=mine,
        default=json.dumps(list(DEFAULT_ARC)),
        words=_centre_words(t),
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
%%css%%
.gate { background:var(--surface); border:1px solid var(--edge);
        border-radius:18px 18px 18px 34px; padding:1.4rem; margin-top:2rem; }
.gate h1 { font-size:1.25rem; margin:0 0 .3rem; }
.gate p { color:var(--ink2); margin:0 0 1rem; font-size:.92rem; }
input[type=password] { width:100%; font:inherit; font-size:1rem; padding:.75rem .9rem;
        border:1px solid var(--line); border-radius:12px; background:var(--paper);
        color:var(--ink); min-height:48px; margin-bottom:.8rem; }
.err { color:var(--terra); font-weight:600; font-size:.9rem; margin:0 0 .8rem; }
.wrap { height:auto; }
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
%%css%%
.votes { margin:0; text-align:center; font-family:PlexMono,monospace; font-size:.7rem;
         letter-spacing:.03em; line-height:1.5; color:var(--ink3); flex:none; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="/static/img/icon.png" alt="" width="28" height="28">
    <a class="name" href="/seasons/review">Sayr</a>
    <span class="left">%%waiting%%</span>
  </header>

  <div class="intro">
    <h1>Что выбрали игроки</h1>
    <p class="lede">Тонкие кольца — ответы, толстая дуга — на чём сошлись.
    Подвиньте края, если знаете лучше, и одобрите.</p>
  </div>

  <form class="play" method="post" action="/seasons/review/approve" %%form_hidden%%>
    <article class="card%%bare%%">
      <img src="%%thumb%%" alt="">
      <div class="cap">
        <h2>%%name%%</h2>
        <p class="meta">%%meta%%</p>
      </div>
    </article>

    <div class="stage">
      <div class="dialbox">%%dial%%</div>
      <div class="plain">
        <label>с <select name="from_month">%%options_from%%</select></label>
        <label>по <select name="to_month">%%options_to%%</select></label>
      </div>
    </div>
    <p class="votes">%%votes%%</p>

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
  var WORDS = %%words%%;
  var go = document.querySelector('[data-go]');
  var picks = form.querySelectorAll('select');
  var svg = document.querySelector('[data-dial]');
  dial(document.querySelector('.dialbox'), function (from, to) {
    picks[0].value = from; picks[1].value = to;
    centreText(svg, from, to, WORDS);
    go.disabled = false;
  }, %%band%%);
})();
</script>
</body>
</html>"""


def render_login(failed: bool = False) -> str:
    return _fill(
        _LOGIN,
        css=SHARED_CSS,
        error='<p class="err">Пароль не подошёл</p>' if failed else "",
    )


def render_review(place, votes: list[tuple[int, int]], waiting: int, enough: int) -> str:
    """Карточка проверки: место, все ответы и предложение."""
    from ..seasons import suggest

    t = RU
    band = suggest(votes) if votes else None
    if place is None:
        meta = name = thumb = slug = votes_line = ""
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
        spread = " · ".join(arc_text(start, end) for start, end in votes)
        mark = " · порог набран" if len(votes) >= enough else ""
        votes_line = f"{len(votes)} отв.{mark}: {spread}"
    centre = centre_lines(*band) if band else ("?", "ВРАЗНОБОЙ")
    return _fill(
        _REVIEW,
        css=SHARED_CSS + CIRCLE_CSS,
        circle_js=CIRCLE_JS,
        waiting=f"ждут проверки: {waiting}" if waiting else "",
        dial=circle_svg(t["months"], t["seasons"], tuple(votes), band, centre),
        votes=escape(votes_line),
        options_from=_options(t, band[0] if band else None),
        options_to=_options(t, band[1] if band else None),
        slug=escape(slug, quote=True),
        name=escape(name),
        meta=escape(meta),
        thumb=escape(thumb, quote=True),
        bare="" if thumb else " bare",
        form_hidden="" if place else "hidden",
        done_hidden="hidden" if place else "",
        go_off="" if band else "disabled",
        band=json.dumps(list(band) if band else None),
        words=_centre_words(t),
    )
