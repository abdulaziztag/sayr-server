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
from collections import Counter
from html import escape

from ..reports import CATEGORY
from ..seasons import (DEFAULT_ARC, FROM_RU, FULL_RU_UP, FULL_UZ_UP, LIMIT_CODES,
                       LIMITS, MONTHS_UZ, SHORT_RU, SHORT_UZ, arc_text, centre_lines,
                       median, touches_winter)

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


def circle_svg(
    short_months: tuple[str, ...],
    seasons: tuple[str, ...],
    band: tuple[int, int] | None = None,
    centre: tuple[str, str] = ("", ""),
) -> str:
    """Круг года целиком, уже в нужном состоянии — скрипт его только двигает."""
    parts = ['<svg class="dial" viewBox="0 0 300 300" data-dial>']
    parts.append(f'<circle class="track" cx="{_MID}" cy="{_MID}" r="{_RING}"/>')
    parts.append(f'<path class="band" data-band d="{band_path(*band) if band else ""}"/>')
    for index, name in enumerate(short_months):
        month = index + 1
        x, y = _pt(_centre(month), _LABELS)
        lit = " on" if band and _inside(month, *band) else ""
        parts.append(
            f'<text class="mon{lit}" data-mon="{month}" x="{x:.1f}" y="{y:.1f}">'
            f"{escape(name)}</text>"
        )
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
.dialbox, .dial, .card, .send {
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
  var MID = 150, R = 104, CAP = 12 / 104 * 180 / Math.PI, drag = null;
  var state = start ? { from: start[0], to: start[1] } : { from: 0, to: 0 };

  // Угол — по часовой от верха. Январь наверху, месяцы идут против часовой
  function centre(m) { return -(m - 1) * 30; }
  function pt(deg) {
    var r = deg * Math.PI / 180;
    return (MID + R * Math.sin(r)).toFixed(1) + ' ' + (MID - R * Math.cos(r)).toFixed(1);
  }
  function span(a, b) { return ((b - a + 12) % 12) + 1; }
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

  // Где палец — в месяцах: 1 — середина января, 2 — февраля, дальше
  // против часовой; дробная часть — насколько он ушёл от середины месяца.
  // Угол нужен, чтобы копить поворот, расстояние — чтобы не слушать середину
  function locate(event) {
    var box = svg.getBoundingClientRect();
    var size = Math.min(box.width, box.height);
    var x = (event.clientX - box.left - box.width / 2) / size * 300;
    var y = (event.clientY - box.top - box.height / 2) / size * 300;
    var ccw = ((-Math.atan2(x, -y) * 180 / Math.PI) % 360 + 360) % 360;
    return { at: ccw / 30 + 1, deg: ccw, dist: Math.sqrt(x * x + y * y) };
  }
  // Разница по кругу, приведённая к промежутку от −half до half
  function turn(d, half) { return ((d + half) % (2 * half) + 2 * half) % (2 * half) - half; }
  // Ручка стоит у края месяца, на CAP внутрь дуги, — в тех же единицах
  function knob(key) {
    return key === 'from' ? state.from - 0.5 + CAP / 30 : state.to + 0.5 - CAP / 30;
  }
  function nearest(at) {
    return Math.abs(turn(knob('from') - at, 6)) < Math.abs(turn(knob('to') - at, 6))
      ? 'from' : 'to';
  }

  function notify() { if (onChange) onChange(state.from, state.to); }

  // Край ходит только в своих пределах: дуга не короче месяца и не длиннее
  // года. Дойдя до соседней, ручка упирается в неё, а не перескакивает —
  // раньше «один месяц» от лёгкого поворота пальца оборачивался «круглым
  // годом». Поэтому палец не ставит месяц напрямую, а копит поворот:
  // u — его положение без обрыва на декабре, и край идёт следом, пока
  // не упрётся
  function take(key, at, deg, tap) {
    var lo = key === 'to' ? state.from : state.to - 11;
    var u;
    if (tap) {
      // Касание переносит край в месяц под пальцем — годится любой,
      // нужна лишь развёртка, где этот месяц попадает в пределы
      u = at + 12 * Math.ceil((lo - Math.floor(at + 0.5)) / 12);
    } else {
      // Палец уже у ручки: развёртка, где он к ней ближе всего
      var now = lo + ((state[key] - lo) % 12 + 12) % 12;
      u = at + 12 * Math.round((now - at) / 12);
    }
    drag = { key: key, lo: lo, u: u, deg: deg };
    follow();
  }
  function follow() {
    var v = Math.min(drag.lo + 11, Math.max(drag.lo, Math.floor(drag.u + 0.5)));
    var m = ((v - 1) % 12 + 12) % 12 + 1;
    if (m === state[drag.key]) return;
    state[drag.key] = m;
    draw();
    notify();
  }

  svg.addEventListener('pointerdown', function (e) {
    var hit = locate(e);
    // Середина круга — это подпись итога, а не место для касания:
    // угол там скачет от малейшего движения, и край дуги дёргался бы
    if (hit.dist < 60) return;
    e.preventDefault();
    svg.setPointerCapture(e.pointerId);
    if (!state.from) {
      state.from = state.to = Math.floor(hit.at + 0.5) % 12 || 12;
      draw();
      notify();
    }
    var n = span(state.from, state.to);
    var mid = knob('from') + turn(knob('to') - knob('from'), 6) / 2;
    if ((n === 1 || n === 12) && Math.abs(turn(hit.at - mid, 6)) < 1) {
      // Ручки слились — в один месяц или в круглый год. Какую тянут,
      // скажет первое движение пальца, а не лишний градус касания
      drag = { wait: true, at: hit.at, deg: hit.deg, moved: 0 };
      return;
    }
    // Двигается БЛИЖАЙШИЙ край дуги, а не начинается выбор заново:
    // промах мимо ручки на телефоне — норма, и сбрасывать из-за него
    // уже поставленную дугу значило бы наказывать за неточный палец
    take(nearest(hit.at), hit.at, hit.deg, true);
  });
  svg.addEventListener('pointermove', function (e) {
    if (!drag) return;
    var hit = locate(e);
    if (hit.dist < 40) return;
    var step = turn(hit.deg - drag.deg, 180);
    drag.deg = hit.deg;
    if (drag.wait) {
      drag.moved += step;
      if (Math.abs(drag.moved) < 3) return;
      // Месяц растёт туда, куда ведут палец: вперёд — конец, назад —
      // начало. Круглый год может только укоротиться — с того края,
      // от которого палец уходит
      var one = span(state.from, state.to) === 1;
      take(one === (drag.moved > 0) ? 'to' : 'from', hit.at, hit.deg, false);
      return;
    }
    if (Math.abs(step) > 90) {
      // Палец пересёк середину круга — такой поворот не копим, а берём
      // ручку заново там, где палец теперь
      take(drag.key, hit.at, hit.deg, false);
      return;
    }
    drag.u += step / 30;
    follow();
  });
  function release(e) {
    // Коснулись слитых ручек и отпустили, не сдвинув: обычное касание
    if (drag && drag.wait && e.type === 'pointerup') {
      take(nearest(drag.at), drag.at, drag.deg, true);
    }
    drag = null;
  }
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

#: Необязательное в игре: зимняя шкала под кругом и шторка с остальным.
#: Главный вопрос остаётся одним касанием — всё здесь можно не трогать
GAME_CSS = """
/* Место под шкалу держится всегда, даже пока она не нужна: иначе круг
   прыгал бы, когда она появляется, — и прямо под пальцем, посреди движения */
.winter { flex:none; border:0; margin:0; padding:0; min-width:0; transition:opacity .18s; }
.js .winter.off { opacity:0; visibility:hidden; transition:opacity .18s, visibility 0s .18s; }
.winter legend, .lab { display:block; padding:0; margin:0 0 .35rem; font-weight:600;
                       font-size:.86rem; color:var(--ink); }
.winter legend em, .lab em { font-style:normal; font-weight:400; font-size:.74rem;
                             color:var(--ink3); margin-left:.3rem; }
.scale { display:grid; grid-template-columns:repeat(10, 1fr); gap:4px; }
.scale label { position:relative; }
.scale input { position:absolute; inset:0; opacity:0; margin:0; cursor:pointer; }
.scale span { display:block; text-align:center; line-height:34px; border-radius:9px;
              background:var(--line); color:var(--ink2); font-family:PlexMono,monospace;
              font-size:.8rem; transition:background .12s, color .12s; }
.scale input:checked + span { background:var(--green); color:var(--surface); font-weight:600; }
.scale input:focus-visible + span { outline:2px solid var(--terra); outline-offset:2px; }
.more { flex:none; }
.more summary { list-style:none; cursor:pointer; display:flex; align-items:center;
                gap:.55rem; min-height:40px; font-size:.88rem; color:var(--ink2); }
.more summary::-webkit-details-marker { display:none; }
.more summary span { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
/* В кружке «+», пока пусто, и число заполненного, когда нет: сколько
   уже сказано, видно, не открывая шторку */
.more summary i { font-style:normal; display:inline-grid; place-items:center; flex:none;
                  width:24px; height:24px; border-radius:50%; border:1px solid var(--line);
                  font-family:PlexMono,monospace; font-size:.8rem; color:var(--ink2); }
.more summary i.on { background:var(--green); border-color:var(--green);
                     color:var(--surface); font-weight:600; }
.sheet .hint { margin:0; font-size:.84rem; color:var(--ink3); }
.sheet { display:flex; flex-direction:column; gap:1rem; padding:.6rem 0 0; }
.sheet .chips { display:flex; flex-wrap:wrap; gap:.4rem; }
.sheet .chip { position:relative; }
.sheet .chip input { position:absolute; inset:0; opacity:0; margin:0; cursor:pointer; }
.sheet .chip span { display:inline-flex; align-items:center; min-height:38px;
                    padding:.35rem .85rem; border:1px solid var(--line); border-radius:19px;
                    font-size:.88rem; color:var(--ink2); background:var(--paper); }
.sheet .chip input:checked + span { background:var(--terra); border-color:var(--terra);
                                    color:#fff; font-weight:600; }
.sheet textarea { width:100%; font:inherit; font-size:.95rem; padding:.65rem .8rem;
                  border:1px solid var(--line); border-radius:12px; background:var(--paper);
                  color:var(--ink); resize:none; }
.scrim, .close { display:none; }
/* Со скриптом шторка выезжает поверх экрана: главный экран остаётся
   без прокрутки, а длинное необязательное живёт отдельно */
.js .more[open] .scrim { display:block; position:fixed; inset:0; z-index:19;
                         background:rgba(12,14,12,.42); }
.js .more[open] .sheet { position:fixed; z-index:20; bottom:0; left:50%;
                         transform:translateX(-50%); width:min(100%, 30rem);
                         max-height:82dvh; overflow:auto;
                         padding:1.1rem 1rem calc(1rem + env(safe-area-inset-bottom));
                         background:var(--surface); border-radius:22px 22px 0 0;
                         box-shadow:0 -12px 40px -18px rgba(0,0,0,.45); }
.js .close { display:block; }
/* На низком экране пояснение под заголовком уступает место кругу */
@media (max-height:700px) { .lede { display:none; } }
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
    "snow": "Тропёжка зимой",
    "snow_hint": "1 — натоптано, 10 — по пояс",
    "more": "Опасность, ограничения, комментарий",
    "optional": "Всё здесь по желанию: отмечайте только то, что знаете точно.",
    "danger": "Опасность",
    "danger_hint": "1 — спокойно, 10 — камнепады, лавины",
    "limits": "Что мешает попасть",
    "note": "Комментарий",
    "note_ph": "Что ещё стоит знать: пропуск, закрытый мост, собаки у кошары…",
    "close": "Закрыть",
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
    "snow": "Qishda iz ochish",
    "snow_hint": "1 — iz bor, 10 — belgacha qor",
    "more": "Xavf, cheklovlar, izoh",
    "optional": "Bu yerda hammasi ixtiyoriy: faqat aniq bilganingizni belgilang.",
    "danger": "Xavf",
    "danger_hint": "1 — xotirjam, 10 — tosh qulashi, koʻchki",
    "limits": "Kirishga nima xalaqit beradi",
    "note": "Izoh",
    "note_ph": "Yana nimani bilish kerak: ruxsatnoma, yopiq koʻprik…",
    "close": "Yopish",
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
<script>document.documentElement.classList.add('js')</script>
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

    <fieldset class="winter%%winter_off%%" data-winter>
      <legend>%%snow_label%% <em>%%snow_hint%%</em></legend>
      <div class="scale">%%snow_scale%%</div>
    </fieldset>

    <details class="more" data-more>
      <summary><i data-more-count aria-hidden="true">+</i><span>%%more_label%%</span></summary>
      <div class="scrim" data-scrim></div>
      <div class="sheet">
        <p class="hint">%%optional%%</p>
        <div><p class="lab">%%danger_label%% <em>%%danger_hint%%</em></p>
          <div class="scale">%%danger_scale%%</div></div>
        <div><p class="lab">%%limits_label%%</p>
          <div class="chips">%%limit_chips%%</div></div>
        <div><label class="lab" for="note">%%note_label%%</label>
          <textarea id="note" name="note" maxlength="300" rows="3"
                    placeholder="%%note_ph%%"></textarea></div>
        <button type="button" class="go close" data-close>%%close_label%%</button>
      </div>
    </details>

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
  var winter = form.querySelector('[data-winter]');
  var more = form.querySelector('[data-more]');
  var moreCount = form.querySelector('[data-more-count]');
  var note = more.querySelector('textarea');

  // Зимняя шкала — только если дуга задевает декабрь, январь или февраль:
  // спрашивать про снег у места, куда ходят с апреля по август, незачем
  function touchesWinter(from, to) {
    var n = ((to - from + 12) % 12) + 1;
    for (var i = 0; i < n; i++) {
      var m = (from - 1 + i) % 12 + 1;
      if (m === 12 || m === 1 || m === 2) return true;
    }
    return false;
  }
  function showWinter(from, to) {
    var on = touchesWinter(from, to);
    winter.classList.toggle('off', !on);
    if (!on) winter.querySelectorAll('input').forEach(function (r) {
      r.checked = false; r.dataset.was = '';
    });
  }

  // Сколько необязательного заполнено — видно, не открывая шторку
  function countMore() {
    var n = 0;
    if (more.querySelector('input[name=danger]:checked')) n++;
    if (more.querySelector('input[name=limits]:checked')) n++;
    if (note.value.trim()) n++;
    moreCount.textContent = n ? String(n) : '+';
    moreCount.classList.toggle('on', !!n);
  }
  function resetMore() {
    form.querySelectorAll('.scale input, .chip input').forEach(function (i) {
      i.checked = false; i.dataset.was = '';
    });
    note.value = '';
    more.open = false;
    countMore();
  }

  // Балл снимается повторным касанием: радиокнопка сама этого не умеет,
  // а необязательное обязано уметь снова стать пустым
  form.querySelectorAll('.scale input').forEach(function (radio) {
    radio.addEventListener('click', function () {
      if (radio.dataset.was === '1') { radio.checked = false; radio.dataset.was = ''; }
      else {
        form.querySelectorAll('input[name="' + radio.name + '"]').forEach(function (o) {
          o.dataset.was = '';
        });
        radio.dataset.was = '1';
      }
      countMore();
    });
  });
  more.addEventListener('change', countMore);
  note.addEventListener('input', countMore);
  form.querySelector('[data-scrim]').addEventListener('click', function () { more.open = false; });
  form.querySelector('[data-close]').addEventListener('click', function () { more.open = false; });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && more.open) more.open = false;
  });

  var knob = dial(document.querySelector('.dialbox'), function (from, to) {
    picks[0].value = from; picks[1].value = to;
    centreText(svg, from, to, WORDS);
    showWinter(from, to);
  }, DEF);
  showWinter(DEF[0], DEF[1]);

  function show(next) {
    form.querySelector('[data-slug]').value = next.slug;
    form.querySelector('[data-name]').textContent = next.name;
    form.querySelector('[data-meta]').textContent = next.meta;
    var img = form.querySelector('[data-thumb]');
    if (next.thumb) { img.src = next.thumb; card.classList.remove('bare'); }
    else { img.removeAttribute('src'); card.classList.add('bare'); }
    // Каждая карточка начинает с одной и той же дуги и с пустого
    // необязательного: иначе ответ на прошлое место подсказывал бы
    // ответ на следующее
    knob.set(DEF[0], DEF[1]);
    picks[0].value = DEF[0]; picks[1].value = DEF[1];
    centreText(svg, DEF[0], DEF[1], WORDS);
    resetMore();
    showWinter(DEF[0], DEF[1]);
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
    // Форма целиком: вместе с дугой уезжает и необязательное, если его
    // заполнили, — даже при «не знаю»: сезон можно не помнить, а про
    // погранзону знать точно
    var body = new FormData(form);
    if (skipped) body.set('skip', '1');
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


def _scale(name: str) -> str:
    """Шкала 1–10 радиокнопками: работает и без скрипта."""
    return "".join(
        f'<label><input type="radio" name="{name}" value="{n}"><span>{n}</span></label>'
        for n in range(1, 11)
    )


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
        css=SHARED_CSS + CIRCLE_CSS + GAME_CSS,
        circle_js=CIRCLE_JS,
        winter_off="" if touches_winter(*DEFAULT_ARC) else " off",
        snow_label=escape(t["snow"]),
        snow_hint=escape(t["snow_hint"]),
        snow_scale=_scale("snow_load"),
        more_label=escape(t["more"]),
        optional=escape(t["optional"]),
        danger_label=escape(t["danger"]),
        danger_hint=escape(t["danger_hint"]),
        danger_scale=_scale("danger"),
        limits_label=escape(t["limits"]),
        limit_chips="".join(
            f'<label class="chip"><input type="checkbox" name="limits" value="{code}">'
            f"<span>{escape(ru if t['lang'] == 'ru' else uz)}</span></label>"
            for code, ru, uz in LIMITS
        ),
        note_label=escape(t["note"]),
        note_ph=escape(t["note_ph"], quote=True),
        close_label=escape(t["close"]),
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
/* Проверка — не игра, а рабочий список: все места сразу, одно касание
   на место. Высота обычная, страница прокручивается */
.wrap { height:auto; min-height:100dvh; max-width:64rem; }
.list { display:flex; flex-direction:column; gap:.55rem; }
.row { display:grid; gap:.5rem .75rem; align-items:center;
       grid-template-columns:52px minmax(0,1fr);
       grid-template-areas:"thumb who" "strip strip" "ctrl ctrl" "facts facts" "said said";
       padding:.7rem .8rem; background:var(--surface); border:1px solid var(--edge);
       border-radius:14px; transition:opacity .22s, transform .22s; }
.row.gone { opacity:0; transform:translateX(24px); }
.row.err { border-color:var(--terra); }
.thumb { grid-area:thumb; width:52px; height:52px; border-radius:10px; object-fit:cover;
         background:var(--line); display:block; }
.who { grid-area:who; min-width:0; }
.who b { display:block; font-size:.98rem; line-height:1.25; white-space:nowrap;
         overflow:hidden; text-overflow:ellipsis; }
.who span { font-family:PlexMono,monospace; font-size:.66rem; letter-spacing:.05em;
            text-transform:uppercase; color:var(--ink3); }
.who .ready { color:var(--green); }
.who .split { color:var(--terra); }
/* Полоска года: цвет месяца — доля игроков, которые его отметили;
   рамка — что уйдёт месту. Разброс мнений читается за полсекунды,
   текст ответов под ней — для точности, а не для чтения */
.strip { grid-area:strip; display:grid; grid-template-columns:repeat(12, 1fr);
         gap:3px; -webkit-user-select:none; user-select:none; }
.strip i { font-style:normal; font-family:PlexMono,monospace; font-size:.62rem;
           text-align:center; line-height:24px; height:24px; border-radius:5px;
           color:var(--ink3); background:var(--line); position:relative; z-index:0; }
.strip i::before { content:""; position:absolute; inset:0; border-radius:5px;
                   background:var(--green); opacity:var(--k); z-index:-1; }
.strip i.hot { color:var(--surface); }
/* Не «pick»: так называется блок списков, и его grid-area утаскивала
   выбранные клетки из полоски стопкой в лишнюю колонку */
.strip i.sel { box-shadow:inset 0 0 0 2px var(--ink); }
.ctrl { grid-area:ctrl; display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; }
.pick { display:flex; align-items:center; gap:.35rem; flex:1 1 15rem; min-width:0; }
.pick select { flex:1; min-width:0; min-height:40px; padding:.35rem .45rem;
               font-size:.88rem; }
.acts { display:flex; gap:.4rem; margin-left:auto; }
.acts button { flex:none; min-height:40px; padding:0 .9rem; font-size:.9rem;
               border-radius:11px; }
.said { grid-area:said; margin:0; font-family:PlexMono,monospace; font-size:.66rem;
        letter-spacing:.02em; color:var(--ink3); }
/* Что ещё сказали игроки: баллы уже выставлены серединой ответов,
   ограничения — отмечены; одобрение применяет их вместе с сезоном */
.facts { grid-area:facts; display:flex; flex-wrap:wrap; align-items:center;
         gap:.4rem .6rem; }
.facts .num { display:flex; align-items:center; gap:.35rem; font-family:PlexMono,monospace;
              font-size:.66rem; letter-spacing:.05em; text-transform:uppercase;
              color:var(--ink3); }
.facts .num select { min-height:34px; padding:.2rem .4rem; font-size:.85rem; }
.facts .flag { position:relative; }
.facts .flag input { position:absolute; inset:0; opacity:0; margin:0; cursor:pointer; }
.facts .flag span { display:inline-flex; align-items:center; min-height:32px;
                    padding:.2rem .7rem; border-radius:16px; border:1px solid var(--line);
                    font-size:.8rem; color:var(--ink2); }
.facts .flag input:checked + span { background:var(--terra); border-color:var(--terra);
                                    color:#fff; }
.facts .notes { flex-basis:100%; margin:0; font-size:.82rem; color:var(--ink2); }
@media (min-width:760px) {
  .row { grid-template-columns:52px minmax(8rem,1fr) minmax(15rem,1.3fr) auto;
         grid-template-areas:"thumb who strip ctrl" "thumb facts facts facts"
                             "thumb said said said"; }
  .ctrl { flex-wrap:nowrap; }
  .pick { flex:none; }
  .pick select { flex:none; width:8.5rem; }
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="/static/img/icon.png" alt="" width="28" height="28">
    <a class="name" href="/seasons/review">Sayr</a>
    <span class="left" data-count>%%waiting%%</span>
  </header>

  <div class="intro">
    <h1>Проверка сезонов</h1>
    <p class="lede">Цвет месяца — сколько игроков его отметили, рамка — что уйдёт
    месту. Поправьте месяцы, если знаете лучше, и одобрите.</p>
  </div>

  <div class="list" data-list>%%rows%%</div>

  <div class="done" data-done %%done_hidden%%>
    <h2>Пока нечего проверять</h2>
    <p>Как только игроки ответят, места появятся здесь.</p>
    <p><a href="/seasons">Открыть игру</a></p>
  </div>
</div>
<script>
(function () {
  var counter = document.querySelector('[data-count]');
  var list = document.querySelector('[data-list]');
  var done = document.querySelector('[data-done]');
  var left = list.querySelectorAll('[data-row]').length;
  function span(a, b) { return ((b - a + 12) % 12) + 1; }
  function inside(m, a, b) { return ((m - a + 12) % 12) < span(a, b); }
  function recount() {
    counter.textContent = left ? 'ждут проверки: ' + left : '';
    if (!left) done.hidden = false;
  }

  list.querySelectorAll('[data-row]').forEach(function (row) {
    var picks = row.querySelectorAll('select');
    var cells = row.querySelectorAll('[data-m]');
    // Рамка идёт за списками: что выбрано в них, то и уйдёт месту
    function paint() {
      var a = +picks[0].value, b = +picks[1].value;
      cells.forEach(function (cell) {
        cell.classList.toggle('sel', !!(a && b) && inside(+cell.getAttribute('data-m'), a, b));
      });
    }
    picks.forEach(function (select) { select.addEventListener('change', paint); });
    paint();

    // Стереть чужие ответы — не то, что делают промахом: первое касание
    // только взводит кнопку, второе в течение двух секунд — стирает
    var clear = row.querySelector('[data-clear]'), armed = null;
    clear.addEventListener('click', function (e) {
      if (armed) return;
      e.preventDefault();
      clear.textContent = 'Точно?';
      armed = setTimeout(function () { armed = null; clear.textContent = 'Очистить'; }, 2000);
    });

    row.addEventListener('submit', function (e) {
      if (!window.fetch) return;
      e.preventDefault();
      var by = e.submitter;
      var url = by && by.hasAttribute('formaction') ? by.formAction : row.action;
      fetch(url, { method: 'POST', body: new FormData(row),
                   headers: { 'Accept': 'application/json' } })
        .then(function (r) { if (!r.ok) throw 0; })
        .then(function () {
          // Строка уезжает сразу: следующая оказывается под пальцем,
          // и разбор идёт касание за касанием, без перезагрузок
          row.classList.add('gone');
          setTimeout(function () { row.remove(); }, 230);
          left -= 1;
          recount();
        })
        .catch(function () { row.classList.add('err'); });
    });
  });
})();
</script>
</body>
</html>"""

#: Буквы месяцев на полоске. «М» и «И» повторяются, но полоска всегда
#: идёт с января по декабрь, и место в ряду снимает двусмысленность
_INITIALS = "ЯФМАМИИАСОНД"


def render_login(failed: bool = False) -> str:
    return _fill(
        _LOGIN,
        css=SHARED_CSS,
        error='<p class="err">Пароль не подошёл</p>' if failed else "",
    )


def _facts(votes: list) -> str:
    """Необязательное из ответов — сводкой, уже готовой к одобрению.

    Баллы — серединой ответов, ограничения — отмечены все, о которых сказал
    хоть кто-то, со счётчиком рядом: погранзону обычно знает один человек
    из трёх, и молчание двух других её не отменяет. Проверяющий снимает
    лишнее одним касанием.

    Скрытые метки «поле было показано» нужны, чтобы одобрение не стирало
    то, чего в строке не было: пустая шкала и отсутствующая шкала — разное
    """
    snow = median([v.snow_load for v in votes if v.snow_load])
    danger = median([v.danger for v in votes if v.danger])
    counts = Counter(code for v in votes for code in (v.limits or []) if code in LIMIT_CODES)
    notes = [v.note for v in votes if v.note]
    if snow is None and danger is None and not counts and not notes:
        return ""

    def number(name: str, label: str, value: int) -> str:
        options = '<option value="">—</option>' + "".join(
            f'<option value="{n}"{" selected" if n == value else ""}>{n}</option>'
            for n in range(1, 11)
        )
        return (f'<label class="num"><span>{label}</span>'
                f'<select name="{name}">{options}</select></label>')

    parts = []
    if snow is not None:
        parts.append(number("winter_load", "Тропёжка", snow))
    if danger is not None:
        parts.append(number("danger", "Опасность", danger))
    if counts:
        parts.append('<input type="hidden" name="limits_shown" value="1">')
        for code, ru, _ in LIMITS:
            if counts[code]:
                parts.append(
                    f'<label class="flag"><input type="checkbox" name="limits" '
                    f'value="{code}" checked><span>{escape(ru)} · {counts[code]}</span></label>'
                )
    if notes:
        parts.append('<p class="notes">' + " · ".join(f"«{escape(n)}»" for n in notes) + "</p>")
    return f'<div class="facts">{"".join(parts)}</div>'


def _row(place, votes: list, enough: int) -> str:
    """Строка списка: место, полоска года, месяцы на одобрение, факты и кнопки."""
    from ..seasons import arc_months, suggest

    arcs = [(v.from_month, v.to_month) for v in votes if v.from_month is not None]
    band = suggest(arcs)
    total = len(arcs)
    count = [0] * 13
    for start, end in arcs:
        for month in arc_months(start, end):
            count[month] += 1
    cells = "".join(
        f'<i data-m="{month}" class="{"hot" if count[month] / total > 0.5 else ""}" '
        f'style="--k:{count[month] / total:.2f}">{_INITIALS[month - 1]}</i>'
        for month in range(1, 13)
    )

    def options(selected: int | None) -> str:
        # Сошлись — месяцы уже выставлены, остаётся одобрить. Нет —
        # списки пустые и обязательные: предлагать чей-то ответ как итог
        # значило бы подсказывать там, где игроки разошлись
        head = "" if selected else '<option value="">—</option>'
        return head + "".join(
            f'<option value="{index + 1}"'
            f'{" selected" if selected == index + 1 else ""}>{escape(name)}</option>'
            for index, name in enumerate(RU["full"])
        )

    cover = place.photos[0] if place.photos else None
    thumb = (cover.thumb_url or cover.url or "") if cover else ""
    image = (f'<img class="thumb" src="{escape(thumb, quote=True)}" alt="" loading="lazy">'
             if thumb else '<div class="thumb"></div>')
    region = place.region.name if place.region else ""
    if band is None:
        state = '<span class="split">вразнобой</span>'
    elif total >= enough:
        state = '<span class="ready">порог набран</span>'
    else:
        state = ""
    info = " · ".join(bit for bit in (escape(region), f"{total} отв.", state) if bit)
    said = " · ".join(arc_text(start, end) for start, end in arcs)
    required = "" if band else " required"
    return (
        f'<form class="row" method="post" action="/seasons/review/approve" data-row>'
        f"{image}"
        f'<div class="who"><b>{escape(place.name)}</b><span>{info}</span></div>'
        f'<div class="strip">{cells}</div>'
        f'<div class="ctrl">'
        f'<div class="pick">'
        f'<select name="from_month" aria-label="С какого месяца"{required}>'
        f'{options(band[0] if band else None)}</select>'
        f"<span>—</span>"
        f'<select name="to_month" aria-label="По какой месяц"{required}>'
        f'{options(band[1] if band else None)}</select>'
        f"</div>"
        f'<div class="acts">'
        f'<button type="submit" class="ghost" data-clear formaction="/seasons/review/clear"'
        f" formnovalidate>Очистить</button>"
        f'<button type="submit" class="go">Одобрить</button>'
        f"</div>"
        f"</div>"
        f'<input type="hidden" name="slug" value="{escape(place.slug, quote=True)}">'
        f"{_facts(votes)}"
        f'<p class="said">{escape(said)}</p>'
        f"</form>"
    )


def render_review(queue: list[tuple[object, list[tuple[int, int]]]], enough: int) -> str:
    """Весь список ждущих проверки — по строке на место."""
    return _fill(
        _REVIEW,
        css=SHARED_CSS,
        waiting=f"ждут проверки: {len(queue)}" if queue else "",
        rows="".join(_row(place, votes, enough) for place, votes in queue),
        done_hidden="hidden" if queue else "",
    )
