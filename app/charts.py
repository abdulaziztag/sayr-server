"""SVG-графики страницы «Статистика»: чистые функции «данные → разметка».

Без JavaScript и без библиотек: страница одна, графиков десяток, и всё,
что им нужно, — линия, столбцы, стопка, воронка и сетка. Значение каждой
точки лежит в `<title>`, браузер показывает его при наведении. Ширина
всегда 100 %, размер задаёт `viewBox`, так что графики тянутся под колонку.

Цвета — токены приложения: зелёный основной ряд, терракота — один акцент
на график, серый для второстепенного. Пустой ряд не рисует нули как факт,
а честно пишет «нет данных»; одна точка остаётся точкой, а не линией
из ниоткуда.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import escape

from markupsafe import Markup

GREEN = "#2F5D3F"
ACCENT = "#C75B12"
INK = "#161A17"
MUTED = "#8A8272"
PAPER = "#F3EEE3"
#: Направляющие и оси — чернила на четверть, чтобы не спорить с данными
GRID = "#161A1726"

#: Все широкие графики рисуются в одной системе координат
W = 600
H = 200
#: Поля: слева под подписи направляющих, снизу под даты
PAD_L, PAD_R, PAD_T, PAD_B = 34, 10, 10, 24


@dataclass(frozen=True)
class Series:
    label: str
    values: list[float]
    color: str = GREEN


def _svg(width: int, height: int, body: str, kind: str) -> Markup:
    return Markup(
        f'<svg class="chart chart-{kind}" viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" xmlns="http://www.w3.org/2000/svg">{body}</svg>'
    )


def _empty(kind: str, width: int = W, height: int = H, text: str = "нет данных") -> Markup:
    return _svg(
        width,
        height,
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
        f'dominant-baseline="middle" fill="{MUTED}" font-size="13">{escape(text)}</text>',
        f"{kind} chart-empty",
    )


def fmt(value: float, unit: str = "") -> str:
    """Число для подписи: целые без хвоста, дроби — с одним знаком через запятую."""
    if float(value).is_integer():
        text = f"{int(value)}"
    else:
        text = f"{value:.1f}".replace(".", ",")
    return f"{text}{unit}"


def _day_label(day: date) -> str:
    return day.strftime("%d.%m")


def _ceiling(values: list[float]) -> float:
    """Верх шкалы: чуть выше максимума и никогда не ноль, иначе деление на ноль
    и график, где единственная точка лежит на потолке."""
    top = max(values, default=0)
    return top * 1.08 if top > 0 else 1.0


def _tick(value: float) -> float:
    """Подпись направляющей: целое, когда счёт идёт на штуки; дробь — только на малых шкалах."""
    return round(value) if value >= 5 else round(value, 1)


def _guides(ceiling: float, x0: float, x1: float, y0: float, y1: float, unit: str = "") -> str:
    """Базовая линия и три направляющих с подписями значений слева."""
    parts = [f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="{INK}" stroke-width="1"/>']
    for i in (1, 2, 3):
        y = y1 - (y1 - y0) * i / 3
        parts.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>'
            f'<text x="{x0 - 4}" y="{y + 3.5:.1f}" text-anchor="end" fill="{MUTED}" '
            f'font-size="10">{fmt(_tick(ceiling * i / 3), unit)}</text>'
        )
    return "".join(parts)


def _date_axis(days: list[date], xs: list[float], y: float) -> str:
    """Даты через равные шаги: не больше шести подписей, крайние — всегда."""
    if not days:
        return ""
    step = max(1, (len(days) - 1) // 5) if len(days) > 1 else 1
    picks = set(range(0, len(days), step)) | {len(days) - 1}
    return "".join(
        f'<text x="{xs[i]:.1f}" y="{y}" text-anchor="middle" fill="{MUTED}" font-size="10">'
        f"{_day_label(days[i])}</text>"
        for i in sorted(picks)
    )


def line(series: list[Series], days: list[date], unit: str = "") -> Markup:
    """Линии по дням. Точка каждого ряда — кружок с подсказкой «дата — ряд: значение»."""
    series = [s for s in series if s.values]
    if not series or not days:
        return _empty("line")
    x0, x1, y0, y1 = PAD_L, W - PAD_R, PAD_T, H - PAD_B
    n = len(days)
    xs = [x0 + (x1 - x0) * (i / (n - 1) if n > 1 else 0.5) for i in range(n)]
    ceiling = _ceiling([v for s in series for v in s.values])
    body = [_guides(ceiling, x0, x1, y0, y1, unit), _date_axis(days, xs, H - 8)]
    for s in series:
        points = [
            (xs[i], y1 - (y1 - y0) * (v / ceiling)) for i, v in enumerate(s.values[:n])
        ]
        if len(points) > 1:
            path = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
            body.append(
                f'<polyline points="{path}" fill="none" stroke="{s.color}" stroke-width="2" '
                f'stroke-linejoin="round" stroke-linecap="round"/>'
            )
        for i, (x, y) in enumerate(points):
            body.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{s.color}">'
                f"<title>{_day_label(days[i])} — {escape(s.label)}: {fmt(s.values[i], unit)}</title>"
                "</circle>"
            )
    if len(series) > 1:
        body.append(_legend([(s.label, s.color) for s in series], x1))
    return _svg(W, H, "".join(body), "line")


def _legend(items: list[tuple[str, str]], right: float) -> str:
    """Легенда в правом верхнем углу: кружок и подпись на ряд."""
    parts = []
    x = right
    for label, color in reversed(items):
        width = 8 + len(label) * 6.2
        x -= width + 10
        parts.append(
            f'<circle cx="{x:.1f}" cy="{PAD_T + 2}" r="3.5" fill="{color}"/>'
            f'<text x="{x + 7:.1f}" y="{PAD_T + 5.5}" fill="{MUTED}" font-size="10">'
            f"{escape(label)}</text>"
        )
    return "".join(parts)


def _clip(label: str, limit: int = 30) -> str:
    return label if len(label) <= limit else label[: limit - 1] + "…"


ROW_H = 24
LABEL_W = 170


def bars(rows: list[tuple[str, float]], horizontal: bool = True, color: str = GREEN,
         unit: str = "") -> Markup:
    """Столбцы: горизонтальные для списков с названиями, вертикальные — для
    коротких осей вроде дней недели."""
    rows = [(str(label), float(value)) for label, value in rows]
    if not rows:
        return _empty("bars", height=80)
    ceiling = _ceiling([v for _, v in rows])
    if horizontal:
        height = PAD_T + ROW_H * len(rows) + 4
        x0, x1 = LABEL_W, W - 48
        body = []
        for i, (label, value) in enumerate(rows):
            y = PAD_T + ROW_H * i
            width = (x1 - x0) * value / ceiling
            body.append(
                f'<text x="{x0 - 8}" y="{y + 15}" text-anchor="end" fill="{INK}" font-size="12">'
                f"{escape(_clip(label))}</text>"
                f'<rect x="{x0}" y="{y + 4}" width="{width:.1f}" height="{ROW_H - 8}" '
                f'rx="3" fill="{color}"><title>{escape(label)}: {fmt(value, unit)}</title></rect>'
                f'<text x="{x0 + width + 6:.1f}" y="{y + 15}" fill="{MUTED}" font-size="11">'
                f"{fmt(value, unit)}</text>"
            )
        return _svg(W, height, "".join(body), "bars")

    x0, x1, y0, y1 = PAD_L, W - PAD_R, PAD_T, H - PAD_B
    slot = (x1 - x0) / len(rows)
    body = [_guides(ceiling, x0, x1, y0, y1, unit)]
    for i, (label, value) in enumerate(rows):
        bar_h = (y1 - y0) * value / ceiling
        bar_w = slot * 0.62
        x = x0 + slot * i + (slot - bar_w) / 2
        body.append(
            f'<rect x="{x:.1f}" y="{y1 - bar_h:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
            f'rx="2" fill="{color}"><title>{escape(label)}: {fmt(value, unit)}</title></rect>'
        )
        if len(rows) <= 12 or i % max(1, len(rows) // 12) == 0:
            body.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{H - 8}" text-anchor="middle" fill="{MUTED}" '
                f'font-size="10">{escape(_clip(label, 8))}</text>'
            )
    return _svg(W, H, "".join(body), "bars")


def paired(rows: list[tuple[str, float, float]], legend: tuple[str, str],
           colors: tuple[str, str] = (GREEN, ACCENT)) -> Markup:
    """Две горизонтальные полосы на строку: «открытия против навигаций»."""
    rows = [(str(label), float(a), float(b)) for label, a, b in rows]
    if not rows:
        return _empty("paired", height=80)
    ceiling = _ceiling([v for _, a, b in rows for v in (a, b)])
    row_h = 30
    height = PAD_T + 14 + row_h * len(rows) + 4
    x0, x1 = LABEL_W, W - 48
    body = [
        f'<circle cx="{x0}" cy="{PAD_T + 2}" r="3.5" fill="{colors[0]}"/>'
        f'<text x="{x0 + 7}" y="{PAD_T + 5.5}" fill="{MUTED}" font-size="10">{escape(legend[0])}</text>'
        f'<circle cx="{x0 + 110}" cy="{PAD_T + 2}" r="3.5" fill="{colors[1]}"/>'
        f'<text x="{x0 + 117}" y="{PAD_T + 5.5}" fill="{MUTED}" font-size="10">{escape(legend[1])}</text>'
    ]
    for i, (label, a, b) in enumerate(rows):
        y = PAD_T + 14 + row_h * i
        body.append(
            f'<text x="{x0 - 8}" y="{y + 18}" text-anchor="end" fill="{INK}" font-size="12">'
            f"{escape(_clip(label))}</text>"
        )
        for j, (value, color, name) in enumerate(((a, colors[0], legend[0]), (b, colors[1], legend[1]))):
            width = (x1 - x0) * value / ceiling
            top = y + 5 + j * 11
            body.append(
                f'<rect x="{x0}" y="{top}" width="{width:.1f}" height="8" rx="2" fill="{color}">'
                f"<title>{escape(label)} — {escape(name)}: {fmt(value)}</title></rect>"
                f'<text x="{x0 + width + 5:.1f}" y="{top + 7.5}" fill="{MUTED}" font-size="9">'
                f"{fmt(value)}</text>"
            )
    return _svg(W, height, "".join(body), "paired")


def stacked(series: list[Series], days: list[date]) -> Markup:
    """Стопка по дням: один столбец на день, сегмент на ряд."""
    series = [s for s in series if s.values and any(s.values)]
    if not series or not days:
        return _empty("stacked")
    n = len(days)
    totals = [sum(s.values[i] if i < len(s.values) else 0 for s in series) for i in range(n)]
    ceiling = _ceiling(totals)
    x0, x1, y0, y1 = PAD_L, W - PAD_R, PAD_T, H - PAD_B
    slot = (x1 - x0) / n
    bar_w = max(2.0, slot * 0.7)
    xs = [x0 + slot * i + slot / 2 for i in range(n)]
    body = [_guides(ceiling, x0, x1, y0, y1), _date_axis(days, xs, H - 8)]
    for i in range(n):
        base = y1
        for s in series:
            value = s.values[i] if i < len(s.values) else 0
            if not value:
                continue
            seg_h = (y1 - y0) * value / ceiling
            base -= seg_h
            body.append(
                f'<rect x="{xs[i] - bar_w / 2:.1f}" y="{base:.1f}" width="{bar_w:.1f}" '
                f'height="{seg_h:.1f}" fill="{s.color}">'
                f"<title>{_day_label(days[i])} — {escape(s.label)}: {fmt(value)}</title></rect>"
            )
    body.append(_legend([(s.label, s.color) for s in series], x1))
    return _svg(W, H, "".join(body), "stacked")


def funnel(steps: list[tuple[str, int]]) -> Markup:
    """Воронка: ширина полосы — доля от первого шага, справа — доля от предыдущего."""
    steps = [(str(label), int(value)) for label, value in steps]
    if not steps or not steps[0][1]:
        return _empty("funnel", height=80)
    first = steps[0][1]
    row_h = 30
    height = PAD_T + row_h * len(steps) + 4
    x0, x1 = LABEL_W, W - 120
    body = []
    previous = first
    for i, (label, value) in enumerate(steps):
        y = PAD_T + row_h * i
        width = (x1 - x0) * value / first
        share = f"{round(100 * value / previous)} %" if previous else "—"
        body.append(
            f'<text x="{x0 - 8}" y="{y + 19}" text-anchor="end" fill="{INK}" font-size="12">'
            f"{escape(_clip(label))}</text>"
            f'<rect x="{x0}" y="{y + 6}" width="{max(width, 1):.1f}" height="{row_h - 12}" rx="3" '
            f'fill="{GREEN if i else INK}" fill-opacity="{1 if i else 0.85}">'
            f"<title>{escape(label)}: {value} ({share} от предыдущего)</title></rect>"
            f'<text x="{x1 + 8}" y="{y + 19}" fill="{INK}" font-size="12">{value}</text>'
            f'<text x="{x1 + 52}" y="{y + 19}" fill="{MUTED}" font-size="11">'
            f'{"" if i == 0 else share}</text>'
        )
        previous = value
    return _svg(W, height, "".join(body), "funnel")


def heatmap(rows: list[str], cols: list[str], values: list[list[float | None]],
            unit: str = "", show_values: bool = True) -> Markup:
    """Сетка: строки × столбцы, заливка по силе, число в клетке, если влезает.

    `None` в клетке — «ещё не наступило» (неделя когорты не закрылась):
    такая клетка не рисуется вовсе, в отличие от честного нуля.
    """
    rows = [str(r) for r in rows]
    cols = [str(c) for c in cols]
    flat = [v for row in values for v in row if v is not None]
    if not rows or not cols or not flat or not any(flat):
        return _empty("heatmap", height=80)
    top = max(flat)
    label_w = 78
    cell_w = (W - label_w - PAD_R) / len(cols)
    cell_h = 22 if cell_w > 26 else 16
    height = 16 + cell_h * len(rows) + 4
    body = []
    for j, col in enumerate(cols):
        if len(cols) <= 16 or j % max(1, len(cols) // 12) == 0:
            body.append(
                f'<text x="{label_w + cell_w * (j + 0.5):.1f}" y="11" text-anchor="middle" '
                f'fill="{MUTED}" font-size="10">{escape(col)}</text>'
            )
    for i, row in enumerate(rows):
        y = 16 + cell_h * i
        body.append(
            f'<text x="{label_w - 6}" y="{y + cell_h / 2 + 3.5:.1f}" text-anchor="end" '
            f'fill="{INK}" font-size="11">{escape(_clip(row, 12))}</text>'
        )
        for j, value in enumerate(values[i][: len(cols)]):
            if value is None:
                continue
            x = label_w + cell_w * j
            strength = value / top if top else 0
            body.append(
                f'<rect x="{x + 1:.1f}" y="{y + 1}" width="{cell_w - 2:.1f}" height="{cell_h - 2}" '
                f'rx="2" fill="{GREEN}" fill-opacity="{0.06 + 0.9 * strength:.2f}">'
                f"<title>{escape(row)} · {escape(cols[j])}: {fmt(value, unit)}</title></rect>"
            )
            if show_values and cell_w > 26 and value:
                ink = PAPER if strength > 0.55 else INK
                body.append(
                    f'<text x="{x + cell_w / 2:.1f}" y="{y + cell_h / 2 + 3.5:.1f}" '
                    f'text-anchor="middle" fill="{ink}" font-size="10">{fmt(value, unit)}</text>'
                )
    return _svg(W, height, "".join(body), "heatmap")


def sparkline(values: list[float], color: str = GREEN) -> Markup:
    """Искорка в плитке: ход за период без осей, последняя точка отмечена."""
    values = [float(v) for v in values]
    width, height = 120, 32
    if not values:
        return _svg(width, height, "", "spark chart-empty")
    ceiling = _ceiling(values)
    n = len(values)
    xs = [3 + (width - 6) * (i / (n - 1) if n > 1 else 0.5) for i in range(n)]
    ys = [height - 4 - (height - 8) * (v / ceiling) for v in values]
    body = []
    if n > 1:
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        body.append(
            f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="1.6" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
    body.append(f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="2.6" fill="{color}"/>')
    summary = f"мин. {fmt(min(values))}, макс. {fmt(max(values))}, последнее {fmt(values[-1])}"
    return _svg(width, height, f"<title>{summary}</title>" + "".join(body), "spark")
