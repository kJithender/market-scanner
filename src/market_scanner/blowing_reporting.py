# ruff: noqa: E501
"""Renderers and the dated archive for the BlowingStocksScreener.

Separate from ``reporting`` because the columns are different — float, tier,
premarket high, VWAP — and because this report keeps a rolling archive that
the others do not. Every run writes one report stamped ``blowing-stocks-
DD-MM-YYYY.*`` directly into the output directory. Anything older than the
retention window is deleted on the next run, so the directory holds exactly
the last week and never grows without bound.

Two rendering rules exist to stop the report from overstating itself. A tier
is printed beside the measurement that earned it, and an unmeasured value is
an em dash — never a zero, never a blank that could read as one.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from market_scanner.blowing_stocks import DISCLAIMER, BlowingStocksResult

BASENAME = "blowing-stocks"

#: Only files this module writes are ever considered for deletion. The pattern
#: is anchored and the date is parsed before anything is removed, so a stray
#: file in the report directory is left alone rather than swept up.
_ARCHIVED = re.compile(rf"^{re.escape(BASENAME)}-(\d{{2}}-\d{{2}}-\d{{4}})\.(json|csv|md|html)$")

_FORMATS = (("json", ".json"), ("csv", ".csv"), ("markdown", ".md"), ("html", ".html"))


def _num(value: float | int | None, *, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}{suffix}"


def _shares(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return f"{value:,.0f}"


def _check(value: bool | None) -> str:
    return "—" if value is None else ("yes" if value else "no")


# ------------------------------------------------------------------- JSON


def render_json(result: BlowingStocksResult) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n"


# -------------------------------------------------------------------- CSV

_CSV_FIELDS = (
    "screen",
    "rank",
    "symbol",
    "tier",
    "score",
    "price",
    "gap_percent",
    "rvol",
    "session_volume",
    "premarket_volume",
    "average_volume",
    "volume_multiple",
    "float_shares",
    "float_basis",
    "float_as_of",
    "float_is_upper_bound",
    "premarket_high",
    "premarket_high_basis",
    "above_premarket_high",
    "vwap",
    "vwap_anchor",
    "above_vwap",
    "previous_close",
    "previous_high",
    "above_previous_high",
    "sma",
    "above_sma",
    "keywords",
    "headline",
    "unproven",
    "rvol_method",
    "data_as_of",
    "feed",
)


def render_csv(result: BlowingStocksResult) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=_CSV_FIELDS, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    for item in (*result.low_float, *result.catalyst):
        row = item.to_dict()
        row["keywords"] = "; ".join(item.keywords)
        row["headline"] = item.headlines[0] if item.headlines else ""
        row["unproven"] = "; ".join(item.unproven)
        writer.writerow(row)
    return stream.getvalue()


# --------------------------------------------------------------- Markdown


def _markdown_table(rows: Sequence[Any], screen: str) -> list[str]:
    if not rows:
        return ["No symbol cleared every gate. The list was not padded.", ""]
    if screen == "low_float":
        header = ["#", "Symbol", "Price", "Gap", "RVOL", "Float", "Vol", ">PM high", ">VWAP", "Tier", "Keywords"]
    else:
        header = ["#", "Symbol", "Price", "RVOL", "Vol x20d", "Gap", ">Yest high", ">SMA", "Tier", "Keywords"]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for item in rows:
        if screen == "low_float":
            cells = [
                str(item.rank),
                f"**{item.symbol}**",
                f"${item.price:,.2f}",
                _num(item.gap_percent, digits=1, suffix="%"),
                _num(item.rvol, digits=1, suffix="x"),
                _shares(item.float_shares) + ("†" if item.float_is_upper_bound else ""),
                _shares(item.session_volume),
                _check(item.above_premarket_high),
                _check(item.above_vwap),
                item.tier,
                ", ".join(item.keywords) or "—",
            ]
        else:
            cells = [
                str(item.rank),
                f"**{item.symbol}**",
                f"${item.price:,.2f}",
                _num(item.rvol, digits=1, suffix="x"),
                _num(item.volume_multiple, digits=1, suffix="x"),
                _num(item.gap_percent, digits=1, suffix="%"),
                _check(item.above_previous_high),
                _check(item.above_sma),
                item.tier,
                ", ".join(item.keywords) or "—",
            ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def render_markdown(result: BlowingStocksResult) -> str:
    lines = [
        "# Blowing Stocks Screener",
        "",
        f"> **{DISCLAIMER}**",
        "",
        f"Session {result.session_date} ({result.session_phase}) · generated {result.generated_at} · "
        f"provider `{result.provider}` feed `{result.feed}` · "
        f"{result.symbols_examined} of {result.universe_size:,} symbols measured · "
        f"{result.symbols_qualified} qualified.",
        "",
        "## 1 · Low-float momentum explosion",
        "",
        *_markdown_table(result.low_float, "low_float"),
        "## 2 · Catalyst + volume breakout",
        "",
        *_markdown_table(result.catalyst, "catalyst"),
    ]
    if any(item.float_is_upper_bound for item in result.low_float):
        lines.extend(
            [
                "† Float shown is shares outstanding, an upper bound. The real float is smaller, "
                "so the threshold was still genuinely cleared.",
                "",
            ]
        )
    lines.append("## Criteria")
    lines.append("")
    for section, values in result.criteria.items():
        lines.append(f"**{section.replace('_', ' ')}**")
        lines.append("")
        lines.extend(f"- *{key.replace('_', ' ')}*: {value}" for key, value in values.items())
        lines.append("")
    if result.warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in result.warnings)
        lines.append("")
    rejections = {
        screen: counts for screen, counts in result.rejection_counts.items() if counts
    }
    if rejections:
        lines.extend(["## Why symbols were rejected", ""])
        for screen, counts in rejections.items():
            top = ", ".join(f"{name} ({count})" for name, count in list(counts.items())[:8])
            lines.append(f"- **{screen}**: {top}")
        lines.append("")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- HTML


def _h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _tier_span(tier: str) -> str:
    return f'<span class="tier {_h(tier)}">{_h(tier)}</span>'


def _flag(value: bool | None) -> str:
    if value is None:
        return '<span class="flat">—</span>'
    return '<span class="up">✓</span>' if value else '<span class="down">✗</span>'


def _low_float_rows(result: BlowingStocksResult) -> str:
    rows = []
    for item in result.low_float:
        headline = item.headlines[0] if item.headlines else ""
        keywords = "".join(f'<span class="kw">{_h(word)}</span>' for word in item.keywords)
        rows.append(
            f"<tr><td><span class='rank'>{item.rank:02d}</span></td>"
            f"<td><strong class='ticker'>{_h(item.symbol)}</strong>{_tier_span(item.tier)}</td>"
            f"<td>${item.price:,.2f}</td>"
            f"<td class='pos'>{_h(_num(item.gap_percent, digits=1, suffix='%'))}</td>"
            f"<td>{_h(_num(item.rvol, digits=1, suffix='x'))}</td>"
            f"<td title=\"{_h(item.float_basis or 'unknown basis')}\">{_h(_shares(item.float_shares))}"
            f"{'<sup>†</sup>' if item.float_is_upper_bound else ''}</td>"
            f"<td>{_h(_shares(item.session_volume))}</td>"
            f"<td title=\"{_h(item.premarket_high_basis)}\">{_flag(item.above_premarket_high)}</td>"
            f"<td title=\"{_h(item.vwap_anchor)}\">{_flag(item.above_vwap)}</td>"
            f"<td>{keywords or '<span class=flat>—</span>'}</td>"
            f"<td class='news' title=\"{_h(headline)}\">{_h(headline[:90])}</td>"
            f"<td>{item.score:,.1f}</td></tr>"
        )
    return "".join(rows) or (
        "<tr><td colspan='99'>No symbol cleared every gate; the list was not padded.</td></tr>"
    )


def _catalyst_rows(result: BlowingStocksResult) -> str:
    rows = []
    for item in result.catalyst:
        headline = item.headlines[0] if item.headlines else ""
        keywords = "".join(f'<span class="kw">{_h(word)}</span>' for word in item.keywords)
        rows.append(
            f"<tr><td><span class='rank'>{item.rank:02d}</span></td>"
            f"<td><strong class='ticker'>{_h(item.symbol)}</strong>{_tier_span(item.tier)}</td>"
            f"<td>${item.price:,.2f}</td>"
            f"<td>{_h(_num(item.rvol, digits=1, suffix='x'))}</td>"
            f"<td>{_h(_num(item.volume_multiple, digits=1, suffix='x'))}</td>"
            f"<td>{_h(_num(item.gap_percent, digits=1, suffix='%'))}</td>"
            f"<td>{_flag(item.above_previous_high)}</td>"
            f"<td>{_flag(item.above_sma)}</td>"
            f"<td>{keywords or '<span class=flat>—</span>'}</td>"
            f"<td class='news' title=\"{_h(headline)}\">{_h(headline[:90])}</td>"
            f"<td>{item.score:,.1f}</td></tr>"
        )
    return "".join(rows) or (
        "<tr><td colspan='99'>No symbol cleared every gate; the list was not padded.</td></tr>"
    )


def render_html(result: BlowingStocksResult, *, title: str = "Blowing Stocks Screener") -> str:
    criteria = "".join(
        "<li><span>"
        + _h(f"{section.replace('_', ' ')} · {key.replace('_', ' ')}")
        + f"</span><strong>{_h(value)}</strong></li>"
        for section, values in result.criteria.items()
        for key, value in values.items()
    )
    warnings = (
        "<section class='warn'><h2>Warnings</h2><p>"
        + "<br>".join(_h(warning) for warning in result.warnings)
        + "</p></section>"
        if result.warnings
        else ""
    )
    rejections = ""
    live = {screen: counts for screen, counts in result.rejection_counts.items() if counts}
    if live:
        blocks = []
        for screen, counts in live.items():
            items = "".join(
                f"<li><span>{_h(name.replace('_', ' '))}</span><strong>{count:,}</strong></li>"
                for name, count in list(counts.items())[:10]
            )
            blocks.append(f"<div><h3>{_h(screen.replace('_', ' '))}</h3><ul>{items}</ul></div>")
        rejections = (
            "<section class='rejects'><h2>Why symbols were rejected</h2>"
            + "".join(blocks)
            + "</section>"
        )
    footnote = (
        "<p class='foot'>† Float shown is shares outstanding, an upper bound on float. "
        "The real float is smaller, so the threshold was genuinely cleared.</p>"
        if any(item.float_is_upper_bound for item in result.low_float)
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_h(title)}</title>
<style>
:root{{--bg:#0b0710;--panel:#150d1c;--line:#3a2a45;--text:#f7f3fa;--muted:#a898b4;--hot:#ff5f8d;--lime:#c9ff63;--cyan:#67e8d2;--amber:#ffc766;--red:#ff7a86}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,"Segoe UI",sans-serif;line-height:1.5}}
.shell{{width:min(1400px,calc(100% - 32px));margin:auto;padding:32px 0 64px}}
h1{{font-size:clamp(1.8rem,4vw,3rem);letter-spacing:-.04em;margin:.3rem 0 .6rem}}
h2{{font-size:1.05rem;letter-spacing:-.01em;margin:30px 0 10px}}
.eyebrow{{margin:0;color:var(--hot);font-size:.72rem;font-weight:800;letter-spacing:.18em;text-transform:uppercase}}
.meta{{color:var(--muted);font-size:.78rem;margin:0 0 18px}}
.disclaimer{{display:flex;gap:12px;padding:15px 18px;border:1px solid #4c4830;border-radius:14px;background:#201d12;color:#ffe8a9;margin:0 0 22px;font-size:.83rem}}
.disclaimer b{{color:var(--amber);white-space:nowrap}}
.criteria{{display:grid;grid-template-columns:repeat(3,1fr);padding:0;margin:0 0 6px;list-style:none;border:1px solid var(--line);border-radius:16px;overflow:hidden;background:rgba(21,13,28,.86)}}
.criteria li{{padding:11px 15px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}}
.criteria li:nth-child(3n){{border-right:0}}
.criteria span{{display:block;color:var(--muted);font-size:.63rem;letter-spacing:.1em;text-transform:uppercase}}
.criteria strong{{font-size:.76rem;font-weight:600}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:16px;background:rgba(21,13,28,.9)}}
table{{width:100%;border-collapse:collapse;min-width:960px;font-variant-numeric:tabular-nums}}
th{{padding:11px 12px;background:#1d1226;color:var(--muted);font-size:.63rem;text-transform:uppercase;letter-spacing:.09em;text-align:left;position:sticky;top:0}}
td{{padding:12px;border-top:1px solid var(--line);font-size:.8rem;vertical-align:middle}}
tbody tr:hover{{background:rgba(255,95,141,.05)}}
.rank{{display:inline-grid;place-items:center;width:28px;height:28px;border:1px solid #5a3a63;border-radius:8px;color:var(--cyan);font-size:.68rem;font-weight:800}}
.ticker{{color:var(--lime);font-size:.95rem}}
.pos{{color:var(--lime);font-weight:700}}
.tier{{display:inline-block;margin-left:8px;padding:2px 7px;border-radius:999px;font-size:.6rem;font-weight:800;text-transform:uppercase;letter-spacing:.08em;border:1px solid var(--line);color:var(--muted)}}
.tier.explosive{{color:#0b0710;background:var(--amber);border-color:var(--amber)}}
.tier.parabolic{{color:#0b0710;background:var(--hot);border-color:var(--hot)}}
.kw{{display:inline-block;margin:1px 3px 1px 0;padding:2px 7px;border-radius:6px;background:#231631;border:1px solid var(--line);color:var(--cyan);font-size:.62rem}}
.news{{max-width:320px;color:var(--muted);font-size:.72rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.up{{color:var(--cyan);font-weight:700}} .down{{color:var(--red)}} .flat{{color:var(--muted)}}
.warn,.rejects{{padding:18px;border:1px solid #66542e;border-radius:14px;background:var(--panel);margin-top:22px}}
.rejects{{border-color:var(--line);display:grid;grid-template-columns:repeat(2,1fr);gap:18px}}
.rejects h2{{grid-column:1/-1;margin:0}}
.rejects h3{{margin:0 0 6px;font-size:.75rem;color:var(--hot);text-transform:uppercase;letter-spacing:.1em}}
.rejects ul{{margin:0;padding:0;list-style:none}}
.rejects li{{display:flex;justify-content:space-between;gap:10px;padding:3px 0;border-bottom:1px dashed var(--line);font-size:.75rem;color:var(--muted)}}
.rejects strong{{color:var(--text)}}
.warn h2{{margin:0 0 .4rem;font-size:1rem}} .warn p{{margin:0;color:var(--muted);font-size:.78rem}}
.foot{{color:var(--muted);font-size:.7rem;margin:8px 2px 0}}
footer{{margin-top:28px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:.7rem}}
@media(max-width:820px){{.criteria{{grid-template-columns:1fr}}.criteria li{{border-right:0}}.rejects{{grid-template-columns:1fr}}}}
@media print{{:root{{--bg:#fff;--panel:#fff;--line:#d3ccd8;--text:#140f18;--muted:#5b5162;--lime:#2c6b1f;--cyan:#12566a;--hot:#a3184a}}body{{background:#fff}}}}
</style>
</head>
<body>
<main class="shell">
  <p class="eyebrow">BlowingStocksScreener · {_h(result.session_phase)} · {_h(result.feed)} feed</p>
  <h1>{_h(title)}</h1>
  <p class="meta">Session {_h(result.session_date)} · generated {_h(result.generated_at)} ·
  provider {_h(result.provider)} · {result.symbols_examined:,} of {result.universe_size:,} listed symbols measured ·
  {result.symbols_qualified} qualified · never padded.</p>
  <aside class="disclaimer"><b>ALREADY PRINTED</b><span>{_h(DISCLAIMER)}</span></aside>
  <ul class="criteria">{criteria}</ul>

  <h2>1 · Low-float momentum explosion</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>#</th><th>Symbol</th><th>Price</th><th>Gap</th>
      <th title="Cumulative volume in today's window vs the same window on prior sessions">RVOL</th>
      <th title="Hover a value for the basis it was derived from">Float</th>
      <th>Volume</th><th>&gt; PM high</th><th>&gt; VWAP</th><th>Keywords</th><th>Headline</th><th>Score</th></tr></thead>
      <tbody>{_low_float_rows(result)}</tbody>
    </table>
  </div>
  {footnote}

  <h2>2 · Catalyst + volume breakout</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>#</th><th>Symbol</th><th>Price</th><th>RVOL</th>
      <th title="Today's volume as a multiple of the 20-session mean">Vol x20d</th>
      <th>Gap</th><th>&gt; Yest high</th><th>&gt; SMA</th><th>Keywords</th><th>Headline</th><th>Score</th></tr></thead>
      <tbody>{_catalyst_rows(result)}</tbody>
    </table>
  </div>

  {warnings}
  {rejections}
  <footer>BlowingStocksScreener · {_h(result.provider)}/{_h(result.feed)} ·
  every figure is a measurement of a session already in progress, not a prediction.</footer>
</main>
</body>
</html>
"""


# ------------------------------------------------------------- persistence


def prune_history(
    directory: str | Path, run_date: date, retention_days: int
) -> list[Path]:
    """Delete dated reports older than the retention window.

    Only files matching the dated pattern this module writes are considered,
    and each one's date is parsed before it is removed, so nothing else that
    happens to live in the directory is ever touched.
    """
    target = Path(directory)
    if not target.is_dir():
        return []
    cutoff = run_date - timedelta(days=retention_days)
    removed: list[Path] = []
    for path in sorted(target.iterdir()):
        if not path.is_file():
            continue
        match = _ARCHIVED.match(path.name)
        if not match:
            continue
        try:
            stamped = datetime.strptime(match.group(1), "%d-%m-%Y").date()
        except ValueError:
            continue
        if stamped <= cutoff:
            path.unlink()
            removed.append(path)
    return removed


def write_reports(
    result: BlowingStocksResult,
    directory: str | Path,
    *,
    retention_days: int = 7,
    run_date: date | None = None,
) -> dict[str, Any]:
    """Write today's dated report and prune anything past the retention window.

    Stamped ``blowing-stocks-DD-MM-YYYY.*`` directly in ``directory`` — no
    separate undated "latest" copy, so the same naming convention applies
    whether this is the only report of the day or an intraday re-run replacing
    it. The stamp is the **run** date rather than the session date: a Saturday
    run reports Friday's session, and stamping it Friday would overwrite
    Friday's own report with a "market closed" page.
    """
    stamp = run_date or date.today()
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)

    rendered = {
        "json": render_json(result),
        "csv": render_csv(result),
        "markdown": render_markdown(result),
        "html": render_html(result),
    }
    paths: dict[str, Path] = {}
    for name, suffix in _FORMATS:
        dated = destination / f"{BASENAME}-{stamp:%d-%m-%Y}{suffix}"
        dated.write_text(rendered[name], encoding="utf-8")
        paths[name] = dated

    pruned = prune_history(destination, stamp, retention_days)
    return {
        "paths": paths,
        "pruned": pruned,
        "retention_days": retention_days,
    }
