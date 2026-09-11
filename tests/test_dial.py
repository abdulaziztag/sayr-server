"""Круг года под пальцем: края дуги не перескакивают друг через друга.

Логика круга живёт в JavaScript (`CIRCLE_JS`), и ошибка в ней видна только
под пальцем: свели ручки, чуть повернули — и «один месяц» стал «круглым
годом». Поэтому круг запускается в Node на поддельном SVG, а палец — это
череда точек на кольце, в месяцах: 1 — середина января, 5.5 — граница
мая и июня.

Без Node тест пропускается: на сервере он не нужен, нужен тому, кто
правит круг.
"""

import json
import shutil
import subprocess

import pytest

from app.api.seasons_page import CIRCLE_JS

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="нет Node")

# SVG из заглушек: круг рисует и читает атрибуты, но ему всё равно, куда
_HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const dial = new Function(input.src + '\nreturn dial;')();
function el() {
  return { a: {}, classList: { toggle() {} }, setAttribute(k, v) { this.a[k] = v; },
           removeAttribute(k) { delete this.a[k]; }, getAttribute(k) { return this.a[k]; } };
}
function run(c) {
  const on = {}, parts = {};
  ['[data-band]', '[data-knob=from]', '[data-knob=to]', '[data-pip=from]', '[data-pip=to]']
    .forEach(s => { parts[s] = el(); });
  const mons = Array.from({ length: 12 }, (_, i) => {
    const m = el(); m.setAttribute('data-mon', String(i + 1)); return m;
  });
  const svg = { querySelector: s => parts[s], querySelectorAll: () => mons,
                getBoundingClientRect: () => ({ left: 0, top: 0, width: 300, height: 300 }),
                addEventListener: (t, f) => { on[t] = f; }, setPointerCapture() {} };
  const seen = [];
  const knob = dial({ querySelector: () => svg }, (f, t) => seen.push([f, t]), c.start);
  for (const [type, at] of c.events) {
    const cw = -(at - 1) * 30 * Math.PI / 180;
    on[type]({ type, pointerId: 1, preventDefault() {},
               clientX: 150 + 104 * Math.sin(cw), clientY: 150 - 104 * Math.cos(cw) });
  }
  return { seen, end: knob.get() };
}
process.stdout.write(JSON.stringify(input.cases.map(run)));
"""


def _run(*cases: dict) -> list[dict]:
    done = subprocess.run(
        ["node", "-e", _HARNESS],
        input=json.dumps({"src": CIRCLE_JS, "cases": list(cases)}),
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(done.stdout)


def _drag(start: float, *stops: float, step: float = 0.05) -> list:
    """Касание в `start` и ведение пальца через `stops` мелкими шагами."""
    events = [["pointerdown", start]]
    here = start
    for stop in stops:
        count = max(1, round(abs(stop - here) / step))
        events += [["pointermove", here + (stop - here) * i / count]
                   for i in range(1, count + 1)]
        here = stop
    return events + [["pointerup", here]]


def _span(state: list[int]) -> int:
    start, end = state
    return (end - start) % 12 + 1


def test_ручка_упирается_в_соседнюю_а_не_перескакивает():
    """Сценарий из жалобы: свели ручки и поводили — года не появляется."""
    (case,) = _run({"start": [4, 6], "events": _drag(6.28, 2.0, 5.0, 2.5)})
    assert case["end"] == [4, 4], "конец упёрся в начало и остался одним месяцем"
    assert {_span(state) for state in case["seen"]} <= {1, 2}, case["seen"]


def test_слитые_в_месяц_ручки_растят_дугу_туда_куда_ведут_палец():
    back, forward = _run(
        {"start": [5, 5], "events": _drag(5.0, 4.2, 5.8, 3.6)},
        {"start": [5, 5], "events": _drag(5.0, 5.8, 4.2, 6.6)},
    )
    assert back["end"] == [4, 5], "назад — растёт начало"
    assert forward["end"] == [5, 7], "вперёд — растёт конец"
    for case in (back, forward):
        assert 12 not in {_span(state) for state in case["seen"]}, case["seen"]


def test_круглый_год_только_укорачивается():
    forward, back = _run(
        {"start": [5, 4], "events": _drag(4.5, 6.2)},
        {"start": [5, 4], "events": _drag(4.5, 2.8)},
    )
    assert forward["end"] == [6, 4] and back["end"] == [5, 3]
    for case in (forward, back):
        assert 1 not in {_span(state) for state in case["seen"]}, case["seen"]


def test_касание_переносит_ближайший_край():
    far, before, drag = _run(
        {"start": [4, 8], "events": [["pointerdown", 11.0], ["pointerup", 11.0]]},
        {"start": [5, 5], "events": [["pointerdown", 4.4], ["pointerup", 4.4]]},
        {"start": [4, 8], "events": _drag(8.28, 11.0)},
    )
    assert far["end"] == [4, 11]
    assert before["end"] == [4, 5], "касание перед слитыми ручками тянет начало, а не делает год"
    assert drag["seen"] == [[4, 9], [4, 10], [4, 11]], "край идёт за пальцем по месяцу"


def test_первое_касание_пустого_круга():
    forward, back = _run(
        {"start": None, "events": _drag(3.0, 6.0)},
        {"start": None, "events": _drag(3.0, 1.0)},
    )
    assert forward["end"] == [3, 6] and back["end"] == [1, 3]
