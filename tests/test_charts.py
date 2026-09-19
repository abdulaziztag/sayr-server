"""Графики — чистые функции: проверяем разметку, а не пиксели."""

from datetime import date, timedelta

from app import charts
from app.charts import Series

DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(7)]


def test_fmt_numbers():
    assert charts.fmt(3) == "3"
    assert charts.fmt(2.5) == "2,5"
    assert charts.fmt(40, " %") == "40 %"
    assert charts.fmt(0.0) == "0"


def test_line_one_title_per_point_with_date_and_value():
    svg = charts.line([Series("Активные", [1, 4, 2, 5, 3, 6, 2])], DAYS)
    assert svg.count("<title>") == 7
    assert "<title>01.09 — Активные: 1</title>" in svg
    assert "<title>07.09 — Активные: 2</title>" in svg
    assert svg.count("<polyline") == 1
    assert 'width="100%"' in svg and "viewBox=" in svg


def test_line_two_series_share_axis_and_carry_legend():
    svg = charts.line([Series("А", [1, 2]), Series("Б", [3, 4], charts.ACCENT)], DAYS[:2])
    assert svg.count("<title>") == 4
    assert svg.count("<polyline") == 2
    assert ">А<" in svg and ">Б<" in svg


def test_line_single_point_is_a_dot_not_a_line():
    svg = charts.line([Series("А", [5])], DAYS[:1])
    assert "<polyline" not in svg
    assert svg.count("<circle") == 1
    assert "<title>01.09 — А: 5</title>" in svg


def test_line_empty_says_no_data():
    assert "нет данных" in charts.line([], DAYS)
    assert "нет данных" in charts.line([Series("А", [])], DAYS)
    assert "chart-empty" in charts.line([Series("А", [1])], [])


def test_bars_horizontal_and_vertical():
    rows = [("Чимган", 12), ("Бельдерсай", 7), ("Нурата <и> Ко", 0)]
    flat = charts.bars(rows)
    assert flat.count("<title>") == 3
    assert "<title>Чимган: 12</title>" in flat
    assert "&lt;и&gt;" in flat and "<и>" not in flat  # чужие названия экранируются
    tall = charts.bars(rows, horizontal=False)
    assert tall.count("<title>") == 3
    assert "нет данных" in charts.bars([])


def test_paired_two_bars_per_row():
    svg = charts.paired([("Вершины", 10, 3), ("Озёра", 4, 0)], ("Открытия", "Навигации"))
    assert svg.count("<title>") == 4
    assert "<title>Вершины — Навигации: 3</title>" in svg


def test_stacked_titles_only_for_nonzero_segments():
    svg = charts.stacked(
        [Series("iOS", [1, 2, 3]), Series("Android", [2, 0, 1], charts.ACCENT)], DAYS[:3]
    )
    assert svg.count("<title>") == 5
    assert "<title>02.09 — iOS: 2</title>" in svg
    assert ">Android<" in svg
    assert "нет данных" in charts.stacked([Series("iOS", [0, 0])], DAYS[:2])


def test_funnel_shows_share_of_previous_step():
    svg = charts.funnel([("Активные", 100), ("Место", 50), ("Пойду", 10)])
    assert svg.count("<title>") == 3
    assert "<title>Место: 50 (50 % от предыдущего)</title>" in svg
    assert "<title>Пойду: 10 (20 % от предыдущего)</title>" in svg
    assert "нет данных" in charts.funnel([("Активные", 0), ("Место", 0)])
    assert "нет данных" in charts.funnel([])


def test_heatmap_skips_unknown_cells_and_keeps_zero():
    svg = charts.heatmap(["пн", "вт"], ["нед. 1", "нед. 2"], [[25, None], [0, 10]], unit=" %")
    assert svg.count("<title>") == 3
    assert "<title>пн · нед. 1: 25 %</title>" in svg
    assert "<title>вт · нед. 1: 0 %</title>" in svg
    assert "нет данных" in charts.heatmap([], [], [])
    assert "нет данных" in charts.heatmap(["пн"], ["0"], [[0]])


def test_sparkline_summary_and_empty():
    svg = charts.sparkline([1, 5, 3])
    assert svg.count("<title>") == 1
    assert "мин. 1, макс. 5, последнее 3" in svg
    assert "<polyline" not in charts.sparkline([4])
    assert "chart-empty" in charts.sparkline([])
