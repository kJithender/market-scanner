"""Generate a single-page wall of live charts for a scanner result CSV.

The scanner tells you which names are moving; it does not show you the tape.
This renders every symbol from a result CSV as a TradingView chart in one
grid, each captioned with the scan metrics that put it on the list, so the
page and the scan cannot drift apart.

The charts are third-party embeds, not project data. TradingView's free tier
serves US equities from Cboe BZX, a single venue, which is real time but is
not the consolidated NBBO tape. Treat the page as a monitor, not as a quote
of record. The generated header repeats this where it will be read.

Usage:
    python scripts/chart_wall.py
    python scripts/chart_wall.py --csv artifacts/market-scan.csv --interval 15
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import html
import json
import webbrowser
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

WIDGET_SRC = (
    "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js"
)

# TradingView resolves a bare ticker, but it resolves against its own default
# exchange ranking and can land on a foreign listing that shares the symbol.
# Pinning the US primary listing removes that ambiguity. Symbols absent from
# this map fall back to the bare ticker rather than guessing an exchange.
EXCHANGES = {
    "MRNA": "NASDAQ", "LCID": "NASDAQ", "MSTR": "NASDAQ", "CLSK": "NASDAQ",
    "COIN": "NASDAQ", "MARA": "NASDAQ", "UPST": "NASDAQ", "MRVL": "NASDAQ",
    "TEAM": "NASDAQ", "FOUR": "NYSE", "HGV": "NYSE",
}

# Columns worth showing under a chart, in display order.
CAPTION_FIELDS = (
    ("atr_percent", "ATR", "{:.2f}%"),
    ("rvol", "RVOL", "{:.2f}x"),
    ("gap_percent", "Gap", "{:+.2f}%"),
    ("volatility_score", "Score", "{:.1f}"),
)


def tv_symbol(symbol: str) -> str:
    exchange = EXCHANGES.get(symbol)
    return f"{exchange}:{symbol}" if exchange else symbol


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if (row.get("symbol") or "").strip()]
    if not rows:
        raise SystemExit(f"No symbols found in {csv_path}")
    return rows


def caption(row: dict[str, str]) -> str:
    parts: list[str] = []
    for key, label, fmt in CAPTION_FIELDS:
        raw = (row.get(key) or "").strip()
        if not raw:
            continue
        try:
            parts.append(f"{label} {fmt.format(float(raw))}")
        except ValueError:
            continue
    trend = (row.get("trend") or "").strip()
    if trend:
        parts.append(trend)
    return " &middot; ".join(html.escape(part) for part in parts)


def price_text(row: dict[str, str]) -> str:
    raw = (row.get("price") or "").strip()
    try:
        return f"${float(raw):,.2f}"
    except ValueError:
        return ""


def widget(row: dict[str, str], interval: str, theme: str) -> str:
    symbol = row["symbol"].strip().upper()
    config = json.dumps(
        {
            "symbol": tv_symbol(symbol),
            "interval": interval,
            "timezone": "America/New_York",
            "theme": theme,
            "style": "1",
            "locale": "en",
            "autosize": True,
            "hide_top_toolbar": False,
            "hide_legend": False,
            "allow_symbol_change": False,
            "save_image": False,
            "studies": ["Volume@tv-basicstudies"],
        },
        indent=2,
    )
    rank = (row.get("rank") or "").strip()
    label = f"#{rank} {symbol}" if rank else symbol
    return f"""      <section class="cell">
        <header class="cell-head">
          <span class="sym">{html.escape(label)}</span>
          <span class="px">{price_text(row)}</span>
        </header>
        <div class="chart">
          <div class="tradingview-widget-container">
            <div class="tradingview-widget-container__widget"></div>
            <script type="text/javascript" src="{WIDGET_SRC}" async>{config}</script>
          </div>
        </div>
        <footer class="cell-foot">{caption(row)}</footer>
      </section>
"""


def build(
    rows: list[dict[str, str]],
    csv_path: Path,
    interval: str,
    theme: str,
    columns: int,
) -> str:
    generated = _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    cells = "".join(widget(row, interval, theme) for row in rows)
    symbol_list = ",".join(row["symbol"].strip().upper() for row in rows)
    return f"""<!doctype html>
<html lang="en" data-theme="{theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chart wall &middot; {len(rows)} symbols</title>
<style>
  :root {{ color-scheme: {theme}; --bg:#0d1117; --panel:#161b22; --line:#30363d;
           --fg:#e6edf3; --muted:#8b949e; --warn:#d29922; }}
  html[data-theme="light"] {{ --bg:#ffffff; --panel:#f6f8fa; --line:#d0d7de;
           --fg:#1f2328; --muted:#59636e; --warn:#9a6700; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
          font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif; }}
  header.page {{ padding:16px 20px; border-bottom:1px solid var(--line); }}
  header.page h1 {{ margin:0 0 4px; font-size:16px; font-weight:600; }}
  .meta {{ color:var(--muted); font-size:12px; }}
  .meta code {{ background:var(--panel); padding:1px 5px; border-radius:4px; }}
  .caveat {{ margin-top:8px; padding:8px 10px; border-left:3px solid var(--warn);
             background:var(--panel); color:var(--muted); font-size:12px;
             max-width:80ch; }}
  .tickers {{ margin-top:8px; font-size:12px; color:var(--muted); }}
  .tickers input {{ width:100%; max-width:80ch; margin-top:4px; padding:6px 8px;
                    background:var(--panel); color:var(--fg);
                    border:1px solid var(--line); border-radius:6px;
                    font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .grid {{ display:grid; gap:12px; padding:12px;
           grid-template-columns:repeat({columns},minmax(0,1fr)); }}
  @media (max-width:1500px) {{ .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
  @media (max-width:900px) {{ .grid {{ grid-template-columns:1fr; }} }}
  .cell {{ background:var(--panel); border:1px solid var(--line); border-radius:8px;
           overflow:hidden; display:flex; flex-direction:column; }}
  .cell-head {{ display:flex; justify-content:space-between; align-items:baseline;
                padding:8px 10px; border-bottom:1px solid var(--line); }}
  .sym {{ font-weight:600; letter-spacing:.02em; }}
  .px {{ color:var(--muted); font-size:12px;
         font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .chart {{ height:420px; }}
  .chart .tradingview-widget-container,
  .chart .tradingview-widget-container__widget {{ height:100%; width:100%; }}
  .cell-foot {{ padding:7px 10px; border-top:1px solid var(--line); color:var(--muted);
                font-size:12px;
                font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
</style>
</head>
<body>
<header class="page">
  <h1>Chart wall &middot; {len(rows)} symbols &middot; {html.escape(interval)}-minute</h1>
  <div class="meta">
    Source <code>{html.escape(csv_path.name)}</code> &middot; page generated {generated}.
    Captions are scan-time values and do <strong>not</strong> update; the charts do.
  </div>
  <div class="caveat">
    Charts are TradingView embeds. On the free tier US equities stream from Cboe
    BZX &mdash; real time, but one venue, not the consolidated NBBO tape, so price
    and volume can both sit below what your broker shows. The scan verified no
    bid/ask at all. Use a broker feed as the quote of record.
  </div>
  <div class="tickers">
    Symbol list for import elsewhere (thinkorswim, broker watchlist):
    <input type="text" readonly value="{html.escape(symbol_list)}" onclick="this.select()">
  </div>
</header>
<div class="grid">
{cells}</div>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a live chart wall from a scan CSV.")
    parser.add_argument(
        "--csv",
        default=str(PROJECT_DIR / "artifacts" / "market-scan-volatility.csv"),
        help="scanner result CSV to chart (default: the high-volatility list)",
    )
    parser.add_argument("--output", help="destination HTML (default: beside the CSV)")
    parser.add_argument(
        "--interval", default="5", help="chart interval in minutes, or D/W (default: 5)"
    )
    parser.add_argument("--theme", default="dark", choices=("dark", "light"))
    parser.add_argument(
        "--columns", type=int, default=3, help="grid columns on a wide screen (default: 3)"
    )
    parser.add_argument(
        "--no-open", action="store_true", help="write the file without opening a browser"
    )
    args = parser.parse_args()

    csv_path = Path(args.csv).resolve()
    if not csv_path.exists():
        raise SystemExit(f"No such CSV: {csv_path}\nRun a scan first.")

    rows = read_rows(csv_path)
    output = (
        Path(args.output).resolve()
        if args.output
        else csv_path.with_name(csv_path.stem + "-charts.html")
    )
    output.write_text(
        build(rows, csv_path, args.interval, args.theme, max(1, args.columns)),
        encoding="utf-8",
    )
    print(f"{len(rows)} charts -> {output}")
    if not args.no_open:
        webbrowser.open(output.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
