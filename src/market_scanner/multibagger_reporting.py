# ruff: noqa: E501
"""Renderers for the multibagger report.

Separate from ``reporting`` because the two reports have different columns and
different meanings: one is a gated watchlist, this is a historical record. The
one-year column is rendered with explicit "rising"/"falling" styling because a
past multiple read without it is the main way this report could mislead.
"""

from __future__ import annotations

import csv
import html
import io
import json
from pathlib import Path
from typing import Any

from market_scanner.multibagger import DISCLAIMER, MultibaggerResult


def _cell(value: float | None, suffix: str = "x") -> str:
    return "—" if value is None else f"{value:.2f}{suffix}"


def _horizons(result: MultibaggerResult) -> list[str]:
    seen: list[str] = []
    for item in result.results:
        for label in item.multiples:
            if label not in seen:
                seen.append(label)
    return sorted(seen, key=lambda label: float(label.rstrip("y")))


def render_json(result: MultibaggerResult) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n"


def render_csv(result: MultibaggerResult) -> str:
    horizons = _horizons(result)
    fields = [
        "rank",
        "symbol",
        "price",
        "average_volume",
        *[f"multiple_{label}" for label in horizons],
        "max_multiple",
        "off_low_multiple",
        "best_horizon",
        "best_multiple",
        "still_rising",
        "history_years",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for item in result.results:
        row = item.to_dict()
        flat = {key: value for key, value in row.items() if key != "multiples"}
        for label in horizons:
            flat[f"multiple_{label}"] = row["multiples"].get(label)
        writer.writerow(flat)
    return stream.getvalue()


def render_markdown(result: MultibaggerResult) -> str:
    horizons = _horizons(result)
    lines = [
        "# Multibagger report",
        "",
        f"> **{DISCLAIMER}**",
        "",
        f"Generated {result.generated_at} · provider `{result.provider}` · "
        f"{result.symbols_qualified} of {result.symbols_scanned} symbols qualified.",
        "",
    ]
    if result.results:
        header = ["#", "Symbol", "Price", *[h.upper() for h in horizons], "Max", "Off low", "Now"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for item in result.results:
            row = item.to_dict()
            trend = "—"
            if row["still_rising"] is not None:
                trend = "rising" if row["still_rising"] else "**falling**"
            cells = [
                str(item.rank),
                item.symbol,
                f"${item.price:,.2f}",
                *[_cell(row["multiples"].get(label)) for label in horizons],
                _cell(item.max_multiple),
                _cell(item.off_low_multiple),
                trend,
            ]
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("No symbol met the configured multiple. The list was not padded.")
    lines.extend(["", "## Criteria", ""])
    lines.extend(f"- **{key}**: {value}" for key, value in result.criteria.items())
    if result.warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in result.warnings)
    return "\n".join(lines) + "\n"


def _h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_html(result: MultibaggerResult, *, title: str = "Multibagger Report") -> str:
    horizons = _horizons(result)
    head = "".join(f"<th>{_h(label.upper())}</th>" for label in horizons)
    rows = []
    for item in result.results:
        row = item.to_dict()
        cells = "".join(f"<td>{_h(_cell(row['multiples'].get(label)))}</td>" for label in horizons)
        if row["still_rising"] is None:
            trend = '<span class="flat">—</span>'
        elif row["still_rising"]:
            trend = '<span class="up">rising</span>'
        else:
            trend = '<span class="down">falling</span>'
        rows.append(
            f"<tr><td><span class='rank'>{item.rank:02d}</span></td>"
            f"<td><strong class='ticker'>{_h(item.symbol)}</strong></td>"
            f"<td>${item.price:,.2f}</td>{cells}"
            f"<td>{_h(_cell(item.max_multiple))}</td>"
            f"<td>{_h(_cell(item.off_low_multiple))}</td>"
            f"<td>{trend}</td>"
            f"<td>{item.average_volume:,.0f}</td></tr>"
        )
    body = (
        "".join(rows)
        or "<tr><td colspan='99'>No symbol met the configured multiple; the list was not padded.</td></tr>"
    )
    criteria = "".join(
        f"<li><span>{_h(key.replace('_', ' ').title())}</span><strong>{_h(value)}</strong></li>"
        for key, value in result.criteria.items()
    )
    warnings = (
        "<section class='warn'><h2>Warnings</h2><p>"
        + "<br>".join(_h(warning) for warning in result.warnings)
        + "</p></section>"
        if result.warnings
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_h(title)}</title>
<style>
:root{{--bg:#07110f;--panel:#0d1b18;--line:#26443c;--text:#f3f7f4;--muted:#98aaa4;--lime:#c9ff63;--cyan:#67e8d2;--red:#ff7a86;--amber:#ffc766}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,"Segoe UI",sans-serif;line-height:1.5}}
.shell{{width:min(1280px,calc(100% - 32px));margin:auto;padding:32px 0 64px}}
h1{{font-size:clamp(1.8rem,4vw,3rem);letter-spacing:-.04em;margin:.3rem 0 1rem}}
.eyebrow{{margin:0;color:var(--lime);font-size:.72rem;font-weight:800;letter-spacing:.18em;text-transform:uppercase}}
.disclaimer{{display:flex;gap:12px;padding:15px 18px;border:1px solid #4c4830;border-radius:14px;background:#201d12;color:#ffe8a9;margin:0 0 22px;font-size:.83rem}}
.disclaimer b{{color:var(--amber);white-space:nowrap}}
.criteria{{display:grid;grid-template-columns:repeat(3,1fr);padding:0;margin:0 0 24px;list-style:none;border:1px solid var(--line);border-radius:16px;overflow:hidden;background:rgba(13,27,24,.86)}}
.criteria li{{padding:12px 16px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}}
.criteria li:nth-child(3n){{border-right:0}} .criteria li:nth-last-child(-n+3){{border-bottom:0}}
.criteria span{{display:block;color:var(--muted);font-size:.66rem;letter-spacing:.1em;text-transform:uppercase}}
.criteria strong{{font-size:.79rem}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:16px;background:rgba(13,27,24,.9)}}
table{{width:100%;border-collapse:collapse;min-width:900px;font-variant-numeric:tabular-nums}}
th{{padding:11px 12px;background:#10251f;color:var(--muted);font-size:.64rem;text-transform:uppercase;letter-spacing:.09em;text-align:left;position:sticky;top:0}}
td{{padding:13px 12px;border-top:1px solid var(--line);font-size:.8rem}}
tbody tr:hover{{background:rgba(103,232,210,.035)}}
.rank{{display:inline-grid;place-items:center;width:28px;height:28px;border:1px solid #3d655a;border-radius:8px;color:var(--cyan);font-size:.68rem;font-weight:800}}
.ticker{{color:var(--lime);font-size:.95rem}}
.up{{color:var(--cyan)}} .down{{color:var(--red);font-weight:700}} .flat{{color:var(--muted)}}
.warn{{padding:18px;border:1px solid #66542e;border-radius:14px;background:var(--panel);margin-top:22px}}
.warn h2{{margin:0 0 .4rem;font-size:1rem}} .warn p{{margin:0;color:var(--muted);font-size:.8rem}}
footer{{margin-top:28px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:.7rem}}
@media(max-width:680px){{.criteria{{grid-template-columns:1fr}}.criteria li{{border-right:0}}}}
@media print{{:root{{--bg:#fff;--panel:#fff;--line:#ccd5d2;--text:#101715;--muted:#53605c;--lime:#235f33;--cyan:#126657}}body{{background:#fff}}}}
</style>
</head>
<body>
<main class="shell">
  <p class="eyebrow">Historical record · not a watchlist</p>
  <h1>{_h(title)}</h1>
  <aside class="disclaimer"><b>ALREADY HAPPENED</b><span>{_h(DISCLAIMER)}</span></aside>
  <ul class="criteria">{criteria}</ul>
  <div class="table-wrap">
    <table>
      <thead><tr><th>#</th><th>Symbol</th><th>Price</th>{head}
      <th title="Multiple over the entire available history">Max</th>
      <th title="Multiple measured from the lowest close on record">Off low</th>
      <th title="Direction over the shortest measured horizon">Now</th>
      <th>ADV</th></tr></thead>
      <tbody>{body}</tbody>
    </table>
  </div>
  {warnings}
  <footer>Generated {_h(result.generated_at)} · provider {_h(result.provider)} ·
  {result.symbols_qualified} of {result.symbols_scanned} symbols qualified · never padded.</footer>
</main>
</body>
</html>
"""


def write_reports(result: MultibaggerResult, directory: str | Path) -> dict[str, Path]:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, suffix, payload in (
        ("json", ".json", render_json(result)),
        ("csv", ".csv", render_csv(result)),
        ("markdown", ".md", render_markdown(result)),
        ("html", ".html", render_html(result)),
    ):
        path = target / f"multibagger{suffix}"
        path.write_text(payload, encoding="utf-8")
        written[name] = path
    return written
