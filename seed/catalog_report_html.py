"""Собирает из отчёта по каталогу страницу, которую не стыдно открыть.

    uv run python -m seed.catalog_report      > ../docs/catalog-status.md
    uv run python -m seed.catalog_report_html   # из .md делает .html

Разметку держим здесь, а не правим руками: цифры в каталоге меняются
каждый раз, когда братик что-то выверяет, и переписывать сорок килобайт
HTML под новые числа — работа, которую делать не надо.

Оформление — те же токены, что в приложении: бумага, чернила, зелёный
и терракота, тот же скруглённый угол у карточек.
"""

import html
import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"
SRC = DOCS / "catalog-status.md"
OUT = DOCS / "catalog-status.html"

LEDE = (
    "Собрано из боевой базы. Списки ниже — то, что стоит пройти глазами: "
    "где данных нет, а где они посчитаны машиной и могут врать."
)

CSS = """:root {
  --paper:#F3EEE3; --surface:#FBF8F1; --edge:#161A1712;
  --ink:#161A17; --ink2:#57524A; --ink3:#8A8272;
  --green:#2F5D3F; --terra:#C75B12; --terra-soft:#C75B121A; --green-soft:#2F5D3F14;
  --sans:ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
}
@media (prefers-color-scheme:dark) {
  :root:not([data-theme="light"]) {
    --paper:#141714; --surface:#1E221E; --edge:#EDEAE11F;
    --ink:#EDEAE1; --ink2:#C6C1B5; --ink3:#8E897D;
    --green:#7FBF95; --terra:#E8843C; --terra-soft:#E8843C24; --green-soft:#7FBF9518;
  }
}
:root[data-theme="dark"] {
  --paper:#141714; --surface:#1E221E; --edge:#EDEAE11F;
  --ink:#EDEAE1; --ink2:#C6C1B5; --ink3:#8E897D;
  --green:#7FBF95; --terra:#E8843C; --terra-soft:#E8843C24; --green-soft:#7FBF9518;
}
*,*::before,*::after { box-sizing:border-box; }
body {
  margin:0; background:var(--paper); color:var(--ink);
  font-family:var(--sans); font-size:15px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
.wrap { max-inline-size:62rem; margin-inline:auto; padding:clamp(1.25rem,4vw,3rem) clamp(1rem,4vw,2rem) 5rem; }
.eyebrow { font-family:var(--mono); font-size:.66rem; letter-spacing:.16em; text-transform:uppercase; color:var(--ink3); margin:0 0 .6rem; }
h1 { font-size:clamp(1.7rem,4.5vw,2.5rem); line-height:1.1; margin:0 0 .5rem; text-wrap:balance; letter-spacing:-.02em; }
.lede { color:var(--ink2); margin:0 0 2rem; max-inline-size:46ch; }
.counts { display:flex; flex-wrap:wrap; gap:.75rem; margin:0 0 2.75rem; padding:0; list-style:none; }
.count { flex:1 1 8rem; background:var(--surface); border:1px solid var(--edge); border-end-start-radius:22px; border-radius:8px 8px 8px 22px; padding:.9rem 1rem 1rem; }
.count__n { font-family:var(--mono); font-size:1.9rem; font-weight:600; line-height:1; font-variant-numeric:tabular-nums; }
.count__l { font-family:var(--mono); font-size:.62rem; letter-spacing:.13em; text-transform:uppercase; color:var(--ink3); margin-block-start:.45rem; display:block; }
section { margin-block-end:2.75rem; }
h2 { font-size:1.12rem; margin:0 0 .35rem; letter-spacing:-.01em; }
h2 .n { font-family:var(--mono); color:var(--ink3); font-weight:500; font-size:.95rem; }
.note { color:var(--ink2); margin:0 0 1rem; font-size:.92rem; max-inline-size:58ch; }
.card { background:var(--surface); border:1px solid var(--edge); border-radius:8px 8px 8px 22px; padding:1rem 1.1rem; }
ul.fill { list-style:none; margin:0; padding:0; display:grid; gap:.55rem; }
.bar { display:grid; grid-template-columns:minmax(7.5rem,10rem) 1fr auto; gap:.75rem; align-items:center; font-size:.88rem; }
.bar__track { block-size:7px; background:var(--edge); border-radius:99px; overflow:hidden; }
.bar__fill { display:block; block-size:100%; background:var(--green); border-radius:99px; }
.bar__fill--warn { background:var(--terra); }
.bar__fill--mid { background:var(--green); opacity:.55; }
.bar__num { font-family:var(--mono); font-variant-numeric:tabular-nums; font-size:.8rem; }
.bar__of { color:var(--ink3); }
.scroll { overflow-x:auto; border:1px solid var(--edge); border-radius:8px 8px 8px 22px; background:var(--surface); }
table { inline-size:100%; border-collapse:collapse; font-size:.87rem; }
th { text-align:start; font-family:var(--mono); font-size:.6rem; letter-spacing:.12em; text-transform:uppercase; color:var(--ink3); font-weight:500; padding:.7rem .85rem; border-block-end:1px solid var(--edge); white-space:nowrap; }
td { padding:.55rem .85rem; border-block-end:1px solid var(--edge); vertical-align:top; }
tr:last-child td { border-block-end:0; }
td.num { font-family:var(--mono); font-variant-numeric:tabular-nums; text-align:end; white-space:nowrap; }
th.num { text-align:end; }
td.dim { color:var(--ink3); white-space:nowrap; }
.chips { display:flex; flex-wrap:wrap; gap:.28rem; }
.chip { font-family:var(--mono); font-size:.63rem; letter-spacing:.04em; padding:.16rem .42rem; border-radius:4px; background:var(--terra-soft); color:var(--terra); white-space:nowrap; }
.chip--ok { background:var(--green-soft); color:var(--green); }
.pill { font-family:var(--mono); font-size:.63rem; letter-spacing:.06em; text-transform:uppercase; padding:.16rem .45rem; border-radius:4px; white-space:nowrap; }
.pill--live { background:var(--green-soft); color:var(--green); }
.pill--draft { background:var(--edge); color:var(--ink3); }
footer { color:var(--ink3); font-size:.82rem; border-block-start:1px solid var(--edge); padding-block-start:1.25rem; }
@media (max-width:640px) { .bar { grid-template-columns:1fr auto; } .bar__track { grid-column:1/-1; order:3; } }"""


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _status(text: str) -> str:
    """«опубликовано»/«черновик» — это состояние, а не слово в ячейке."""
    kind = "live" if text == "опубликовано" else "draft"
    return f'<span class="pill pill--{kind}">{html.escape(text)}</span>'


def _chips(text: str) -> str:
    """Чего не хватает. Пусто — значит всё на месте, и это тоже видно."""
    if not text or text.startswith("—"):
        return '<span class="chip chip--ok">готово</span>'
    parts = [p.strip() for p in text.split(",") if p.strip()]
    inner = "".join(f'<span class="chip">{html.escape(p)}</span>' for p in parts)
    return f'<div class="chips">{inner}</div>'


def _bars(rows: list[list[str]], total: int) -> str:
    """Полнота данных читается полосами, а не колонкой чисел."""
    out = []
    for label, have, _ in rows:
        n = int(have)
        pct = round(n / total * 100) if total else 0
        mod = "--ok" if n == total else ("--warn" if pct < 50 else "--mid")
        out.append(
            f'    <li class="bar">\n'
            f'      <span class="bar__label">{html.escape(label)}</span>\n'
            f'      <span class="bar__track"><span class="bar__fill bar__fill{mod}"'
            f' style="inline-size:{pct}%"></span></span>\n'
            f'      <span class="bar__num">{n}<span class="bar__of">/{total}</span></span>\n'
            f"    </li>"
        )
    return '<div class="card"><ul class="fill">\n' + "\n".join(out) + "\n</ul></div>"


def _table(header: list[str], rows: list[list[str]]) -> str:
    """Числовые колонки выравниваем по правому краю — их сравнивают глазами."""
    numeric = {"фото", "трек", "треков", "всего", "опубликовано", "дорога", "ход",
               "ход по формуле", "длина"}
    align = [" class=\"num\"" if h.lower() in numeric else "" for h in header]
    th = "".join(f"<th{a}>{html.escape(h)}</th>" for h, a in zip(header, align))
    body = []
    for r in rows:
        tds = []
        for i, c in enumerate(r):
            if c in ("опубликовано", "черновик"):
                tds.append(f"<td>{_status(c)}</td>")
            elif header[i].lower() == "чего не хватает":
                tds.append(f"<td>{_chips(c)}</td>")
            else:
                a = align[i] if c not in ("—", "") else ""
                tds.append(f"<td{a}>{html.escape(c)}</td>")
        body.append("      <tr>" + "".join(tds) + "</tr>")
    return (
        '<div class="scroll"><table>\n'
        f"      <thead><tr>{th}</tr></thead>\n      <tbody>\n"
        + "\n".join(body)
        + "\n      </tbody>\n    </table></div>"
    )


def build(md: str) -> str:
    lines = md.splitlines()
    m = re.search(r"Всего мест \*\*(\d+)\*\*: опубликовано (\d+), черновиков (\d+)", md)
    total, pub, drafts = (int(g) for g in m.groups()) if m else (0, 0, 0)

    parts = [
        "<title>Каталог Sayr — что проверить</title>",
        f"<style>\n{CSS}\n</style>",
        '\n<div class="wrap">',
        '  <p class="eyebrow">Sayr · инвентаризация каталога</p>',
        "  <h1>Что готово и что нужно проверить</h1>",
        f'  <p class="lede">{LEDE}</p>',
        '\n  <ul class="counts">',
        f'    <li class="count"><span class="count__n">{total}</span>'
        '<span class="count__l">мест всего</span></li>',
        f'    <li class="count"><span class="count__n">{pub}</span>'
        '<span class="count__l">опубликовано</span></li>',
        f'    <li class="count"><span class="count__n">{drafts}</span>'
        '<span class="count__l">черновиков</span></li>',
        "  </ul>",
    ]

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("## "):
            i += 1
            continue
        title = line[3:].strip()
        # «Черновики — 5» и «Далеко … (32)»: число уводим в приглушённый счётчик
        num = ""
        if (mm := re.search(r"^(.*?)\s*[—-]\s*(\d+)$", title)) or (
            mm := re.search(r"^(.*?)\s*\((\d+)\)$", title)
        ):
            title, num = mm.group(1).strip(), mm.group(2)
        head = html.escape(title) + (f' <span class="n">{num}</span>' if num else "")

        i += 1
        notes, header, rows = [], None, []
        while i < len(lines) and not lines[i].startswith("## "):
            cur = lines[i]
            if cur.startswith("|"):
                cells = _cells(cur)
                if set("".join(cells)) <= set("-: "):
                    pass
                elif header is None:
                    header = cells
                else:
                    rows.append(cells)
            elif cur.strip():
                notes.append(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(cur.strip())))
            i += 1

        parts.append("\n  <section>")
        parts.append(f"    <h2>{head}</h2>")
        for n in notes:
            parts.append(f'    <p class="note">{n}</p>')
        if header:
            if title == "Полнота данных":
                parts.append("    " + _bars(rows, total))
            else:
                parts.append("    " + _table(header, rows))
        parts.append("  </section>")

    parts.append("</div>")
    return "\n".join(parts) + "\n"


def main() -> None:
    OUT.write_text(build(SRC.read_text("utf-8")), encoding="utf-8")
    print(f"  {OUT.relative_to(DOCS.parent)} — {OUT.stat().st_size // 1024} КБ")


if __name__ == "__main__":
    main()
