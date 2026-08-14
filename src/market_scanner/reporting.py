# ruff: noqa: E501
"""Dependency-free reporting for market scanner results.

The public renderers accept plain mappings, dataclasses, Pydantic-style models,
or objects with attributes.  This keeps reporting isolated from the scanner's
data-provider and domain-model choices.
"""

from __future__ import annotations

import csv
import html
import io
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

DISCLAIMER = (
    "Research only — not investment advice. Verify all prices, catalysts, "
    "liquidity, and risk parameters with a live broker feed before trading."
)

DEFAULT_FILTERS: dict[str, str] = {
    "price": "$5–$150",
    "volume": ">1,000,000 shares",
    "spread": "Tight spread",
    "atr": "2–5%",
    "beta": ">1",
    "rvol": ">1.5",
    "technical_structure": "Clean trend and clear levels",
    "catalyst": "Earnings, news, or gap",
    "watchlist_size": "10–15 stocks maximum",
}

CSV_FIELDS = (
    "rank",
    "symbol",
    "company",
    "price",
    "volume",
    "current_volume",
    "rvol",
    "atr_percent",
    "beta",
    "spread_percent",
    "gap_percent",
    "trend",
    "levels",
    "catalysts",
    "thesis",
    "stop",
    "target",
    "risk",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    for method_name in ("model_dump", "dict", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            converted = method()
            if isinstance(converted, Mapping):
                return converted
    data = getattr(value, "__dict__", None)
    if isinstance(data, Mapping):
        return {key: val for key, val in data.items() if not key.startswith("_")}
    return {}


def _get(data: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _number(value: Any) -> float | int | None:
    if value in (None, "", "N/A", "n/a"):
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        number = float(str(value).replace("$", "").replace(",", "").rstrip("%"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _timestamp(value: Any, *, fallback_now: bool = False) -> str | None:
    if value is None:
        if not fallback_now:
            return None
        value = datetime.now(UTC)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (list, tuple, set)):
        return "; ".join(_text(item) for item in value if item is not None)
    if isinstance(value, Mapping):
        return "; ".join(f"{key}: {_text(val)}" for key, val in value.items())
    return str(value)


def _catalysts(value: Any, row: Mapping[str, Any]) -> list[str]:
    if value is None:
        derived: list[str] = []
        if _get(row, "earnings_date", "next_earnings"):
            derived.append(f"Earnings {_get(row, 'earnings_date', 'next_earnings')}")
        if _get(row, "news", "headline"):
            derived.append(_text(_get(row, "news", "headline")))
        gap = _number(_get(row, "gap_percent", "gap_pct", "gap"))
        if gap is not None:
            derived.append(f"Gap {gap:+g}%")
        return derived
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Mapping):
        return [f"{key}: {_text(val)}" for key, val in value.items() if val is not None]
    if isinstance(value, Iterable):
        return [_text(item) for item in value if item is not None]
    return [_text(value)]


def _target_watchlist_size(filters: Mapping[str, Any]) -> str:
    """Read the configured cap from the scanner's own filter description.

    The cap is configurable, so it must not be hard-coded in the report.
    """
    digits = "".join(
        character for character in _text(filters.get("watchlist_size")) if character.isdigit()
    )
    return digits or "10–15"


def _candidate(value: Any, position: int) -> dict[str, Any]:
    row = _mapping(value)
    checklist = _mapping(_get(row, "pre_trade_checklist", "checklist", "trade_plan"))
    levels = _get(row, "levels", "key_levels")
    if levels is None:
        support = _get(row, "support", "support_level")
        resistance = _get(row, "resistance", "resistance_level")
        levels = {
            key: val
            for key, val in (("support", support), ("resistance", resistance))
            if val is not None
        }
    return {
        "rank": int(_number(_get(row, "rank", default=position)) or position),
        "symbol": _text(_get(row, "symbol", "ticker"), "—").upper(),
        "company": _text(_get(row, "company", "company_name", "name"), ""),
        "price": _number(_get(row, "price", "last", "last_price", "close")),
        "volume": _number(_get(row, "avg_volume", "average_volume", "volume")),
        "current_volume": _number(_get(row, "current_volume", "premarket_volume")),
        "rvol": _number(_get(row, "rvol", "relative_volume", "relative_volume_ratio")),
        "atr_percent": _number(_get(row, "atr_percent", "atr_pct", "atr_percentage")),
        "beta": _number(_get(row, "beta")),
        "spread_percent": _number(_get(row, "spread_percent", "spread_pct", "bid_ask_spread_pct")),
        "gap_percent": _number(_get(row, "gap_percent", "gap_pct", "gap")),
        "trend": _text(_get(row, "trend", "trend_quality", "technical_structure"), "Not supplied"),
        "levels": _text(levels, "Not supplied"),
        "catalysts": _catalysts(_get(row, "catalysts", "catalyst"), row),
        "thesis": _text(_get(checklist, "thesis", default=_get(row, "thesis")), "Needs review"),
        "stop": _text(
            _get(checklist, "stop", "stop_loss", default=_get(row, "stop", "stop_loss")),
            "Needs review",
        ),
        "target": _text(
            _get(
                checklist, "target", "profit_target", default=_get(row, "target", "profit_target")
            ),
            "Needs review",
        ),
        "risk": _text(
            _get(checklist, "risk", "risk_amount", default=_get(row, "risk", "risk_amount")),
            "Needs review",
        ),
        "rvol_method": _text(_get(row, "rvol_method"), "Not supplied"),
        "score": _number(_get(row, "score")),
        "passed_filters": dict(_mapping(_get(row, "passed_filters"))),
        "catalyst_details": list(_get(row, "catalyst_details", default=[]) or []),
    }


def normalize_scan_result(result: Any) -> dict[str, Any]:
    """Return a stable, JSON-serializable report schema.

    Candidates are ranked and capped at 15. Existing explicit ranks win;
    otherwise order is preserved. Error and empty states remain reportable.
    """

    root = _mapping(result)
    raw_candidates = _get(
        root, "candidates", "results", "stocks", "watchlist", "symbols", default=[]
    )
    if raw_candidates is None:
        raw_candidates = []
    if isinstance(raw_candidates, (Mapping, str, bytes)):
        raw_candidates = [raw_candidates]
    try:
        candidates = [
            _candidate(value, index) for index, value in enumerate(raw_candidates, start=1)
        ]
    except TypeError:
        candidates = []
    candidates.sort(key=lambda row: row["rank"])
    candidates = candidates[:15]
    for index, candidate in enumerate(candidates, start=1):
        candidate["rank"] = index

    supplied_filters = _mapping(_get(root, "filters", "applied_filters", "criteria"))
    filters = {
        str(key): _text(value) for key, value in (supplied_filters or DEFAULT_FILTERS).items()
    }
    errors_value = _get(root, "errors", "error", default=[])
    if isinstance(errors_value, str):
        errors = [errors_value]
    elif errors_value is None:
        errors = []
    else:
        try:
            errors = [_text(item) for item in errors_value]
        except TypeError:
            errors = [_text(errors_value)]
    warnings_value = _get(root, "warnings", default=[])
    if isinstance(warnings_value, str):
        warnings = [warnings_value]
    else:
        try:
            warnings = [_text(item) for item in (warnings_value or [])]
        except TypeError:
            warnings = [_text(warnings_value)]

    data_as_of = _timestamp(_get(root, "data_as_of", "as_of", "market_data_as_of", "freshness"))
    return {
        "schema_version": "1.0",
        "generated_at": _timestamp(
            _get(root, "generated_at", "generation_time"), fallback_now=True
        ),
        "data_as_of": data_as_of,
        "data_freshness": _text(
            _get(root, "data_freshness", "freshness_label"),
            f"Market data as of {data_as_of}"
            if data_as_of
            else "Data timestamp not supplied — verify freshness",
        ),
        "timezone": _text(_get(root, "timezone", "schedule_timezone"), "America/Los_Angeles"),
        "filters": filters,
        "candidate_count": len(candidates),
        "target_watchlist_size": _target_watchlist_size(filters),
        "status": "error" if errors else ("empty" if not candidates else "ok"),
        "candidates": candidates,
        "errors": errors,
        "warnings": warnings,
        "provider": _text(_get(root, "provider"), "unknown"),
        "symbols_scanned": _number(_get(root, "symbols_scanned")),
        "symbols_qualified": _number(_get(root, "symbols_qualified")),
        "rejection_counts": dict(_mapping(_get(root, "rejection_counts"))),
        "disclaimer": DISCLAIMER,
    }


def render_json(result: Any, *, indent: int | None = 2) -> str:
    """Render normalized machine-readable JSON."""

    return (
        json.dumps(
            normalize_scan_result(result), indent=indent, ensure_ascii=False, allow_nan=False
        )
        + "\n"
    )


def render_csv(result: Any) -> str:
    """Render one flat row per candidate for spreadsheet ingestion."""

    report = normalize_scan_result(result)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    for candidate in report["candidates"]:
        row = dict(candidate)
        row["catalysts"] = "; ".join(candidate["catalysts"])
        writer.writerow(row)
    return stream.getvalue()


def _display_number(value: Any, *, prefix: str = "", suffix: str = "", decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{prefix}{float(value):,.{decimals}f}{suffix}"


def _md(value: Any) -> str:
    return _text(value, "—").replace("|", "\\|").replace("\n", " ")


def render_markdown(result: Any) -> str:
    """Render a concise human-readable morning watchlist."""

    report = normalize_scan_result(result)
    lines = [
        "# Morning Market Scanner",
        "",
        f"> **{report['disclaimer']}**",
        "",
        f"Generated: `{report['generated_at']}`  ",
        f"Freshness: {report['data_freshness']}  ",
        f"Schedule timezone: `{report['timezone']}`  ",
        f"Watchlist: **{report['candidate_count']} / {report['target_watchlist_size']} max**",
        "",
        "## Applied filters",
        "",
    ]
    lines.extend(
        f"- **{_md(key.replace('_', ' ').title())}:** {_md(value)}"
        for key, value in report["filters"].items()
    )
    lines.extend(["", "## Ranked watchlist", ""])
    if report["errors"]:
        lines.append("**Scanner error:** " + "; ".join(_md(error) for error in report["errors"]))
        lines.append("")
    if not report["candidates"]:
        lines.extend(["No stocks met every filter. No trade is a valid outcome.", ""])
    else:
        lines.extend(
            [
                "| # | Symbol | Price | Volume | RVOL | ATR | Beta | Spread | Gap | Trend | Levels | Catalysts |",
                "|---:|:---|---:|---:|---:|---:|---:|---:|---:|:---|:---|:---|",
            ]
        )
        for row in report["candidates"]:
            lines.append(
                "| {rank} | **{symbol}** | {price} | {volume} | {rvol} | {atr} | {beta} | {spread} | {gap} | {trend} | {levels} | {catalysts} |".format(
                    rank=row["rank"],
                    symbol=_md(row["symbol"]),
                    price=_display_number(row["price"], prefix="$"),
                    volume=_display_number(row["volume"], decimals=0),
                    rvol=_display_number(row["rvol"], suffix="×"),
                    atr=_display_number(row["atr_percent"], suffix="%"),
                    beta=_display_number(row["beta"]),
                    spread=_display_number(row["spread_percent"], suffix="%"),
                    gap=_display_number(row["gap_percent"], suffix="%"),
                    trend=_md(row["trend"]),
                    levels=_md(row["levels"]),
                    catalysts=_md(row["catalysts"] or ["None supplied"]),
                )
            )
        lines.extend(["", "## Pre-trade checklist", ""])
        for row in report["candidates"]:
            lines.extend(
                [
                    f"### {row['rank']}. {_md(row['symbol'])}",
                    "",
                    f"- **Thesis:** {_md(row['thesis'])}",
                    f"- **Stop:** {_md(row['stop'])}",
                    f"- **Target:** {_md(row['target'])}",
                    f"- **Risk:** {_md(row['risk'])}",
                    "",
                ]
            )
    if report["warnings"]:
        lines.extend(
            ["## Warnings", ""] + [f"- {_md(value)}" for value in report["warnings"]] + [""]
        )
    return "\n".join(lines).rstrip() + "\n"


def _h(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _metric(value: Any, *, prefix: str = "", suffix: str = "", decimals: int = 2) -> str:
    return _h(_display_number(value, prefix=prefix, suffix=suffix, decimals=decimals))


def render_html(result: Any, *, title: str = "Morning Market Scanner") -> str:
    """Render a responsive, self-contained HTML dashboard."""

    report = normalize_scan_result(result)
    filter_chips = "".join(
        f"<li><span>{_h(key.replace('_', ' ').title())}</span><strong>{_h(value)}</strong></li>"
        for key, value in report["filters"].items()
    )
    alerts = ""
    if report["errors"]:
        alerts += (
            '<section class="state error" role="alert"><h2>Scanner error</h2><p>'
            + "<br>".join(_h(error) for error in report["errors"])
            + "</p></section>"
        )
    if report["warnings"]:
        alerts += (
            '<section class="state warning"><h2>Warnings</h2><p>'
            + "<br>".join(_h(warning) for warning in report["warnings"])
            + "</p></section>"
        )

    table_rows: list[str] = []
    checklist_cards: list[str] = []
    for row in report["candidates"]:
        badges = "".join(f'<span class="badge">{_h(value)}</span>' for value in row["catalysts"])
        if not badges:
            badges = '<span class="badge muted">None supplied</span>'
        table_rows.append(
            f"""
            <tr>
              <td data-label="Rank"><span class="rank">{row["rank"]:02d}</span></td>
              <td data-label="Stock"><strong class="ticker">{_h(row["symbol"])}</strong><small>{_h(row["company"])}</small></td>
              <td data-label="Price">{_metric(row["price"], prefix="$")}</td>
              <td data-label="Volume">{_metric(row["volume"], decimals=0)}</td>
              <td data-label="RVOL">{_metric(row["rvol"], suffix="×")}</td>
              <td data-label="ATR">{_metric(row["atr_percent"], suffix="%")}</td>
              <td data-label="Beta">{_metric(row["beta"])}</td>
              <td data-label="Spread">{_metric(row["spread_percent"], suffix="%")}</td>
              <td data-label="Gap">{_metric(row["gap_percent"], suffix="%")}</td>
              <td data-label="Structure"><strong>{_h(row["trend"])}</strong><small>{_h(row["levels"])}</small></td>
              <td data-label="Catalyst"><div class="badges">{badges}</div></td>
            </tr>"""
        )
        checklist_cards.append(
            f"""
            <article class="trade-card">
              <header><span>{row["rank"]:02d}</span><h3>{_h(row["symbol"])}</h3></header>
              <dl>
                <div><dt>Thesis</dt><dd>{_h(row["thesis"])}</dd></div>
                <div><dt>Stop</dt><dd>{_h(row["stop"])}</dd></div>
                <div><dt>Target</dt><dd>{_h(row["target"])}</dd></div>
                <div><dt>Risk</dt><dd>{_h(row["risk"])}</dd></div>
              </dl>
            </article>"""
        )

    if table_rows:
        watchlist_content = f"""
        <div class="table-wrap">
          <table>
            <thead><tr><th>#</th><th>Stock</th><th>Price</th><th title="20-session average daily volume">Volume (ADV)</th><th>RVOL</th><th>ATR</th><th>Beta</th><th>Spread</th><th>Gap</th><th>Structure / levels</th><th>Catalyst</th></tr></thead>
            <tbody>{"".join(table_rows)}</tbody>
          </table>
        </div>"""
        checklist_content = '<div class="trade-grid">' + "".join(checklist_cards) + "</div>"
    else:
        watchlist_content = """
        <section class="state empty">
          <div class="empty-icon">∅</div>
          <h2>No qualifying setups</h2>
          <p>No stocks met every active filter. Preserve capital; check again on the next scheduled run.</p>
        </section>"""
        checklist_content = (
            '<p class="muted-copy">Checklist unavailable until a qualifying setup appears.</p>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>{_h(title)}</title>
  <style>
    :root{{--bg:#07110f;--panel:#0d1b18;--panel2:#10231f;--line:#26443c;--text:#f3f7f4;--muted:#98aaa4;--lime:#c9ff63;--cyan:#67e8d2;--red:#ff7a86;--amber:#ffc766;--shadow:0 20px 60px rgba(0,0,0,.3)}}
    *{{box-sizing:border-box}} html{{scroll-behavior:smooth}} body{{margin:0;background:radial-gradient(circle at 75% -10%,#153c33 0,transparent 34rem),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5}}
    body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.055;background-image:linear-gradient(rgba(255,255,255,.3) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.3) 1px,transparent 1px);background-size:40px 40px}}
    .shell{{width:min(1520px,calc(100% - 32px));margin:auto;padding:36px 0 64px;position:relative}}
    .eyebrow{{margin:0;color:var(--lime);font-size:.72rem;font-weight:800;letter-spacing:.18em;text-transform:uppercase}} h1{{font-size:clamp(2rem,5vw,4.6rem);line-height:.98;letter-spacing:-.055em;margin:.45rem 0 1.1rem;max-width:850px}} h2{{font-size:1.05rem;margin:0;letter-spacing:-.01em}} .hero{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:28px;align-items:end;margin-bottom:22px}}
    .meta{{display:grid;grid-template-columns:repeat(2,minmax(150px,1fr));border:1px solid var(--line);border-radius:18px;background:rgba(13,27,24,.8);box-shadow:var(--shadow);overflow:hidden}} .meta div{{padding:14px 18px;border-bottom:1px solid var(--line)}} .meta div:nth-child(odd){{border-right:1px solid var(--line)}} .meta div:nth-last-child(-n+2){{border-bottom:0}} .meta span,.section-head p,small,.muted-copy{{display:block;color:var(--muted);font-size:.75rem}} .meta strong{{font-size:.88rem}}
    .disclaimer{{display:flex;gap:12px;align-items:flex-start;padding:15px 18px;border:1px solid #4c4830;border-radius:14px;background:#201d12;color:#ffe8a9;margin:0 0 22px;font-size:.83rem}} .disclaimer b{{color:var(--amber);white-space:nowrap}}
    .filters{{display:grid;grid-template-columns:repeat(3,1fr);padding:0;margin:0 0 28px;list-style:none;border:1px solid var(--line);border-radius:18px;overflow:hidden;background:rgba(13,27,24,.86)}} .filters li{{padding:12px 16px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}} .filters li:nth-child(3n){{border-right:0}} .filters li:nth-last-child(-n+3){{border-bottom:0}} .filters span{{display:block;color:var(--muted);font-size:.66rem;letter-spacing:.1em;text-transform:uppercase}} .filters strong{{font-size:.79rem}}
    section.block{{margin-top:30px}} .section-head{{display:flex;align-items:end;justify-content:space-between;margin-bottom:10px}} .section-head p{{margin:0}} .table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:18px;background:rgba(13,27,24,.9);box-shadow:var(--shadow)}} table{{width:100%;border-collapse:collapse;min-width:1160px;font-variant-numeric:tabular-nums}} th{{padding:11px 12px;background:#10251f;color:var(--muted);font-size:.64rem;text-transform:uppercase;letter-spacing:.09em;text-align:left;position:sticky;top:0}} td{{padding:15px 12px;border-top:1px solid var(--line);font-size:.78rem;vertical-align:top}} tbody tr:hover{{background:rgba(103,232,210,.035)}} .rank{{display:inline-grid;place-items:center;width:29px;height:29px;border:1px solid #3d655a;border-radius:8px;color:var(--cyan);font-size:.69rem;font-weight:800}} .ticker{{display:block;color:var(--lime);font-size:.96rem;letter-spacing:.035em}} .badges{{display:flex;flex-wrap:wrap;gap:5px;max-width:220px}} .badge{{display:inline-block;padding:3px 7px;border:1px solid #40665b;border-radius:999px;background:#132b25;color:var(--cyan);font-size:.65rem;white-space:nowrap}} .badge.muted{{color:var(--muted)}}
    .trade-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}} .trade-card{{border:1px solid var(--line);border-radius:16px;background:linear-gradient(145deg,var(--panel2),var(--panel));overflow:hidden}} .trade-card header{{display:flex;gap:10px;align-items:center;padding:13px 15px;border-bottom:1px solid var(--line)}} .trade-card header span{{color:var(--cyan);font:700 .68rem ui-monospace,SFMono-Regular,monospace}} .trade-card h3{{margin:0;color:var(--lime);font-size:.92rem}} dl{{margin:0}} dl div{{display:grid;grid-template-columns:60px 1fr;gap:10px;padding:10px 15px;border-bottom:1px solid rgba(38,68,60,.55)}} dl div:last-child{{border:0}} dt{{color:var(--muted);font-size:.67rem;text-transform:uppercase;letter-spacing:.06em}} dd{{margin:0;font-size:.76rem}}
    .state{{padding:22px;border:1px solid var(--line);border-radius:16px;background:var(--panel);margin:12px 0}} .state p{{margin:.45rem 0 0;color:var(--muted)}} .state.error{{border-color:#6f3740;background:#231215}} .state.error h2{{color:var(--red)}} .state.warning{{border-color:#66542e}} .empty{{text-align:center;padding:50px 20px}} .empty-icon{{display:grid;place-items:center;width:48px;height:48px;margin:0 auto 14px;border:1px solid var(--line);border-radius:50%;color:var(--muted)}} footer{{margin-top:34px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:.7rem}}
    @media(max-width:1000px){{.hero{{grid-template-columns:1fr}}.meta{{width:100%}}.trade-grid{{grid-template-columns:repeat(2,1fr)}}}}
    @media(max-width:680px){{.shell{{width:min(100% - 20px,1520px);padding-top:24px}}.filters{{grid-template-columns:1fr 1fr}}.filters li,.filters li:nth-child(3n){{border-right:1px solid var(--line);border-bottom:1px solid var(--line)}}.filters li:nth-child(2n){{border-right:0}}.filters li:nth-last-child(-n+2){{border-bottom:0}}.trade-grid{{grid-template-columns:1fr}}.section-head{{align-items:start;flex-direction:column;gap:2px}}}}
    @media(prefers-reduced-motion:reduce){{html{{scroll-behavior:auto}}}}
    @media print{{:root{{--bg:#fff;--panel:#fff;--panel2:#fff;--line:#ccd5d2;--text:#101715;--muted:#53605c;--lime:#235f33;--cyan:#126657}}body{{background:#fff}}body:before{{display:none}}.shell{{width:100%;padding:0}}.table-wrap,.meta{{box-shadow:none}}}}
  </style>
</head>
<body>
  <main class="shell">
    <header class="hero">
      <div><p class="eyebrow">06:00 PT · Decision Brief</p><h1>{_h(title)}</h1></div>
      <div class="meta">
        <div><span>Generated</span><strong>{_h(report["generated_at"])}</strong></div>
        <div><span>Data freshness</span><strong>{_h(report["data_freshness"])}</strong></div>
        <div><span>Timezone</span><strong>{_h(report["timezone"])}</strong></div>
        <div><span>Watchlist</span><strong>{report["candidate_count"]} / {report["target_watchlist_size"]} max</strong></div>
      </div>
    </header>
    <aside class="disclaimer"><b>RESEARCH ONLY</b><span>{_h(report["disclaimer"])}</span></aside>
    {alerts}
    <ul class="filters" aria-label="Applied scanner filters">{filter_chips}</ul>
    <section class="block" aria-labelledby="watchlist-heading">
      <header class="section-head"><h2 id="watchlist-heading">Ranked watchlist</h2><p>Maximum 15 · strongest qualifying setups first</p></header>
      {watchlist_content}
    </section>
    <section class="block" aria-labelledby="checklist-heading">
      <header class="section-head"><h2 id="checklist-heading">Pre-trade checklist</h2><p>Thesis · stop · target · defined risk</p></header>
      {checklist_content}
    </section>
    <footer>Generated by Market Scanner · Validate execution data independently · {report["candidate_count"]} qualifying securities</footer>
  </main>
</body>
</html>
"""


def write_reports(
    result: Any, output_dir: str | Path, *, basename: str = "market-scan"
) -> dict[str, Path]:
    """Write all report formats and return paths keyed by format."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    renderers = {
        "json": render_json,
        "csv": render_csv,
        "markdown": render_markdown,
        "html": render_html,
    }
    suffixes = {"json": ".json", "csv": ".csv", "markdown": ".md", "html": ".html"}
    paths: dict[str, Path] = {}
    for format_name, renderer in renderers.items():
        path = destination / f"{basename}{suffixes[format_name]}"
        path.write_text(renderer(result), encoding="utf-8")
        paths[format_name] = path
    return paths


__all__ = [
    "CSV_FIELDS",
    "DEFAULT_FILTERS",
    "DISCLAIMER",
    "normalize_scan_result",
    "render_csv",
    "render_html",
    "render_json",
    "render_markdown",
    "write_reports",
]
