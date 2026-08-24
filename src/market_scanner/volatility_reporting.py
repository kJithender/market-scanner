# ruff: noqa: E501
"""Renderers for the separate High-volatility report.

The market-scan report only ever shows names that passed every hard gate.
This report is the opposite population: names screened on liquidity and ATR
alone, which have **not** passed those gates and carry no thesis, stop, or
target. Keeping them in entirely separate files — not a section bolted onto
market-scan's own json/md/html — means a reader skimming the wrong report can
never mistake an unscreened name for a gated one.

Written every time ``market-scanner scan`` runs, whether or not any name
qualifies: an honest "none today" page beats a missing file, and it matches
how the main market-scan report itself already handles an empty watchlist.
"""

from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path
from typing import Any

from market_scanner.reporting import normalize_scan_result, render_volatility_csv

BASENAME = "High-volatility"

DISCLAIMER = (
    "Screened on liquidity and ATR only. These names have NOT passed the hard gates "
    "and carry no thesis, stop, or target. This is a starting point for manual work, "
    "not a watchlist."
)

#: Columns shown, in order, across the markdown table and the HTML table.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("price", "Price"),
    ("atr_percent", "ATR"),
    ("rvol", "RVOL"),
    ("spread_percent", "Spread"),
    ("gap_percent", "Gap"),
    ("volume_confirmation", "Vol conf"),
    ("trend", "Trend"),
    ("volatility_score", "Score"),
)


def _report(result: Any) -> dict[str, Any]:
    return normalize_scan_result(result)


def render_json(result: Any, *, indent: int | None = 2) -> str:
    """Machine-readable form: metadata plus every high-volatility row.

    A thin projection of ``normalize_scan_result``, not a copy of its schema —
    this file should read as its own report, not as a leftover fragment of
    the scan's JSON.
    """
    report = _report(result)
    payload = {
        "generated_at": report["generated_at"],
        "data_as_of": report["data_as_of"],
        "data_freshness": report["data_freshness"],
        "provider": report["provider"],
        "volatility_count": report["volatility_count"],
        "candidates": report["volatility_candidates"],
        "disclaimer": DISCLAIMER,
    }
    return json.dumps(payload, indent=indent, ensure_ascii=False, allow_nan=False) + "\n"


def render_csv(result: Any) -> str:
    """Delegates to ``reporting.render_volatility_csv``: same rows, same
    columns, just filed under this report's own name now."""
    return render_volatility_csv(result)


def _display_number(value: Any, *, prefix: str = "", suffix: str = "", decimals: int = 2) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{prefix}{number:,.{decimals}f}{suffix}"


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(result: Any) -> str:
    report = _report(result)
    rows = report["volatility_candidates"]
    lines = [
        "# High-volatility report",
        "",
        f"> **{DISCLAIMER}**",
        "",
        f"Generated {report['generated_at']} · provider `{report['provider']}` · "
        f"{report['volatility_count']} name{'s' if report['volatility_count'] != 1 else ''} screened.",
        "",
    ]
    if rows:
        header = ["#", "Symbol", *[label for _, label in _COLUMNS], "On watchlist"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for row in rows:
            cells = [str(row["rank"]), f"**{_md(row['symbol'])}**"]
            for key, _ in _COLUMNS:
                value = row[key]
                if key in ("atr_percent", "spread_percent", "gap_percent"):
                    cells.append(_display_number(value, suffix="%"))
                elif key in ("rvol", "volume_confirmation"):
                    cells.append(_display_number(value, suffix="×"))
                elif key == "price":
                    cells.append(_display_number(value, prefix="$"))
                elif key == "volatility_score":
                    cells.append(_display_number(value, decimals=1))
                else:
                    cells.append(_md(value))
            cells.append("yes" if row["on_watchlist"] else "no")
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("No high-volatility names on this run. The list was not padded.")
    return "\n".join(lines).rstrip() + "\n"


def _h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _metric(value: Any, *, prefix: str = "", suffix: str = "", decimals: int = 2) -> str:
    return _h(_display_number(value, prefix=prefix, suffix=suffix, decimals=decimals))


def render_html(result: Any, *, title: str = "High-Volatility Report") -> str:
    report = _report(result)
    rows = report["volatility_candidates"]

    body_rows = []
    for row in rows:
        flag = (
            '<span class="badge">on watchlist</span>'
            if row["on_watchlist"]
            else '<span class="badge muted">screen only</span>'
        )
        body_rows.append(
            f"""
            <tr>
              <td><span class="rank">{row["rank"]:02d}</span></td>
              <td><strong class="ticker">{_h(row["symbol"])}</strong></td>
              <td>{_metric(row["price"], prefix="$")}</td>
              <td class="hot">{_metric(row["atr_percent"], suffix="%")}</td>
              <td>{_metric(row["rvol"], suffix="×")}</td>
              <td>{_metric(row["spread_percent"], suffix="%")}</td>
              <td>{_metric(row["gap_percent"], suffix="%")}</td>
              <td>{_metric(row["volume_confirmation"], suffix="×")}</td>
              <td>{_h(row["trend"])}</td>
              <td>{_metric(row["volatility_score"], decimals=1)}</td>
              <td>{flag}</td>
            </tr>"""
        )
    if body_rows:
        table = f"""
        <div class="table-wrap">
          <table>
            <thead><tr><th>#</th><th>Stock</th><th>Price</th><th title="14-session average true range as a percent of price">ATR</th><th>RVOL</th><th>Spread</th><th>Gap</th><th title="Recent mean volume over the equally long window before it; above 1.00 means participation is expanding">Vol conf</th><th>Trend</th><th title="Volatility ranking only; not comparable to the watchlist score">Score</th><th></th></tr></thead>
            <tbody>{"".join(body_rows)}</tbody>
          </table>
        </div>"""
    else:
        table = """
        <section class="state empty">
          <div class="empty-icon">∅</div>
          <h2>No high-volatility names</h2>
          <p>Nothing cleared the liquidity and ATR screen on this run.</p>
        </section>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{_h(title)}</title>
<style>
:root{{--bg:#07110f;--panel:#0d1b18;--line:#26443c;--text:#f3f7f4;--muted:#98aaa4;--lime:#c9ff63;--cyan:#67e8d2;--red:#ff7a86;--amber:#ffc766}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,"Segoe UI",sans-serif;line-height:1.5}}
.shell{{width:min(1280px,calc(100% - 32px));margin:auto;padding:32px 0 64px}}
.eyebrow{{margin:0;color:var(--lime);font-size:.72rem;font-weight:800;letter-spacing:.18em;text-transform:uppercase}}
h1{{font-size:clamp(1.8rem,4vw,3rem);letter-spacing:-.04em;margin:.3rem 0 1rem}}
.meta{{color:var(--muted);font-size:.82rem;margin:0 0 20px}}
.disclaimer{{display:flex;gap:12px;align-items:flex-start;padding:15px 18px;border:1px solid #4c4830;border-radius:14px;background:#201d12;color:#ffe8a9;margin:0 0 22px;font-size:.83rem}}
.disclaimer b{{color:var(--amber);white-space:nowrap}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:16px;background:rgba(13,27,24,.9)}}
table{{width:100%;border-collapse:collapse;min-width:900px;font-variant-numeric:tabular-nums}}
th{{padding:11px 12px;background:#10251f;color:var(--muted);font-size:.64rem;text-transform:uppercase;letter-spacing:.09em;text-align:left;position:sticky;top:0}}
td{{padding:13px 12px;border-top:1px solid var(--line);font-size:.8rem}}
tbody tr:hover{{background:rgba(103,232,210,.035)}}
.rank{{display:inline-grid;place-items:center;width:28px;height:28px;border:1px solid #3d655a;border-radius:8px;color:var(--cyan);font-size:.68rem;font-weight:800}}
.ticker{{color:var(--lime);font-size:.95rem}}
.hot{{color:var(--red)}}
.badge{{display:inline-block;padding:3px 7px;border:1px solid #40665b;border-radius:999px;background:#132b25;color:var(--cyan);font-size:.65rem;white-space:nowrap}}
.badge.muted{{color:var(--muted)}}
.state.empty{{text-align:center;padding:50px 20px;border:1px solid var(--line);border-radius:16px;background:var(--panel)}}
.empty-icon{{display:grid;place-items:center;width:48px;height:48px;margin:0 auto 14px;border:1px solid var(--line);border-radius:50%;color:var(--muted)}}
.state.empty p{{color:var(--muted);margin:.4rem 0 0}}
footer{{margin-top:28px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:.7rem}}
@media print{{:root{{--bg:#fff;--panel:#fff;--line:#ccd5d2;--text:#101715;--muted:#53605c;--lime:#235f33;--cyan:#126657}}body{{background:#fff}}}}
</style>
</head>
<body>
<main class="shell">
  <p class="eyebrow">Screen only · not a watchlist</p>
  <h1>{_h(title)}</h1>
  <p class="meta">Generated {_h(report["generated_at"])} · provider {_h(report["provider"])} · {report["data_freshness"]}</p>
  <aside class="disclaimer"><b>NOT GATED</b><span>{_h(DISCLAIMER)}</span></aside>
  {table}
  <footer>{report["volatility_count"]} name{"s" if report["volatility_count"] != 1 else ""} on this run · never padded.</footer>
</main>
</body>
</html>
"""


def write_reports(
    result: Any, directory: str | Path, *, run_date: date | None = None
) -> dict[str, Path]:
    """Write all four formats, stamped ``High-volatility-DD-MM-YYYY.ext``.

    Always writes, whether or not any name qualifies, so the report is
    reachable from the index on every run rather than appearing and
    disappearing with the count.
    """
    stamp = run_date or date.today()
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, suffix, payload in (
        ("json", ".json", render_json(result)),
        ("csv", ".csv", render_csv(result)),
        ("markdown", ".md", render_markdown(result)),
        ("html", ".html", render_html(result)),
    ):
        path = target / f"{BASENAME}-{stamp:%d-%m-%Y}{suffix}"
        path.write_text(payload, encoding="utf-8")
        written[name] = path
    return written


__all__ = [
    "BASENAME",
    "DISCLAIMER",
    "render_csv",
    "render_html",
    "render_json",
    "render_markdown",
    "write_reports",
]
