# ruff: noqa: E501
"""Builds the parent index page linking to every generated screener report.

Each screener (market-scan, multibagger, blowing-stocks) writes its own dated
files directly into the shared output directory; nothing there tracks what
else is present. This module scans that directory and rebuilds one
``index.html`` page listing every report it finds, grouped by screener and
sorted newest first, so an operator has one link to reach any of them.

Rebuilt from disk on every call rather than maintained incrementally: the
directory is the source of truth (including blowing-stocks' own retention
pruning, which deletes files independently of anything here), so scanning it
fresh is simpler than keeping a second copy of that state in sync.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

INDEX_FILENAME = "index.html"

#: Screener basename -> display label, also fixing the section order below.
SCREENERS: dict[str, str] = {
    "market-scan": "Market Scan",
    "High-volatility": "High Volatility",
    "multibagger": "Multibagger",
    "blowing-stocks": "Blowing Stocks",
}

_REPORT = re.compile(
    r"^(?P<screener>market-scan|High-volatility|multibagger|blowing-stocks)-"
    r"(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4})\.(?P<ext>json|csv|md|html)$"
)

_FORMAT_LABELS = (("html", "Dashboard"), ("md", "Markdown"), ("csv", "CSV"), ("json", "JSON"))


@dataclass(frozen=True)
class ReportEntry:
    screener: str
    run_date: date
    formats: dict[str, str]  # extension -> filename (relative, for linking)


def _parse_date(match: re.Match[str]) -> date | None:
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError:
        # A malformed or out-of-range date (e.g. day 32) is not a report this
        # module wrote; skip it rather than crash the whole index build.
        return None


def discover_reports(directory: str | Path) -> list[ReportEntry]:
    """Scan ``directory`` for dated report files and group them by run.

    Only filenames matching the exact pattern each screener writes are
    considered, so unrelated files (caches, logs, a stray CSV) are ignored
    rather than mis-listed.
    """
    target = Path(directory)
    if not target.is_dir():
        return []

    grouped: dict[tuple[str, date], dict[str, str]] = defaultdict(dict)
    for path in sorted(target.iterdir()):
        if not path.is_file():
            continue
        match = _REPORT.match(path.name)
        if not match:
            continue
        stamp = _parse_date(match)
        if stamp is None:
            continue
        grouped[(match["screener"], stamp)][match["ext"]] = path.name

    entries = [
        ReportEntry(screener=screener, run_date=stamp, formats=formats)
        for (screener, stamp), formats in grouped.items()
    ]
    entries.sort(key=lambda entry: (entry.screener, entry.run_date), reverse=True)
    return entries


def _h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_index(entries: list[ReportEntry], *, generated_at: str) -> str:
    by_screener: dict[str, list[ReportEntry]] = defaultdict(list)
    for entry in entries:
        by_screener[entry.screener].append(entry)
    for rows in by_screener.values():
        rows.sort(key=lambda entry: entry.run_date, reverse=True)

    sections = []
    for screener, label in SCREENERS.items():
        rows = by_screener.get(screener, [])
        if not rows:
            sections.append(
                f"<section class='block'><h2>{_h(label)}</h2>"
                f"<p class='empty'>No reports found yet.</p></section>"
            )
            continue
        items = []
        for index, entry in enumerate(rows):
            links = " · ".join(
                f"<a href='{_h(name)}'>{_h(fmt_label)}</a>"
                for ext, fmt_label in _FORMAT_LABELS
                if (name := entry.formats.get(ext))
            )
            badge = "<span class='latest'>Latest</span>" if index == 0 else ""
            items.append(
                f"<li><span class='date'>{entry.run_date:%d %b %Y}</span>{badge}"
                f"<span class='links'>{links}</span></li>"
            )
        sections.append(
            f"<section class='block'><h2>{_h(label)}"
            f"<span class='count'>{len(rows)} report{'s' if len(rows) != 1 else ''}</span></h2>"
            f"<ul class='reports'>{''.join(items)}</ul></section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Screener Reports</title>
<style>
:root{{--bg:#07110f;--panel:#0d1b18;--line:#26443c;--text:#f3f7f4;--muted:#98aaa4;--lime:#c9ff63;--cyan:#67e8d2}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,"Segoe UI",sans-serif;line-height:1.5}}
.shell{{width:min(920px,calc(100% - 32px));margin:auto;padding:36px 0 64px}}
.eyebrow{{margin:0;color:var(--lime);font-size:.72rem;font-weight:800;letter-spacing:.18em;text-transform:uppercase}}
h1{{font-size:clamp(1.8rem,4vw,2.6rem);letter-spacing:-.03em;margin:.3rem 0 .6rem}}
.meta{{color:var(--muted);font-size:.82rem;margin:0 0 28px}}
.block{{border:1px solid var(--line);border-radius:16px;background:rgba(13,27,24,.9);padding:18px 22px;margin-bottom:18px}}
.block h2{{margin:0 0 12px;font-size:1.05rem;display:flex;align-items:center;gap:10px}}
.count{{color:var(--muted);font-size:.72rem;font-weight:400}}
.empty{{color:var(--muted);font-size:.85rem;margin:0}}
ul.reports{{list-style:none;margin:0;padding:0}}
ul.reports li{{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:10px 0;border-top:1px solid var(--line)}}
ul.reports li:first-child{{border-top:0}}
.date{{font-variant-numeric:tabular-nums;min-width:110px;font-size:.86rem}}
.latest{{background:var(--lime);color:#07110f;font-size:.62rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;padding:2px 8px;border-radius:999px}}
.links{{margin-left:auto;font-size:.82rem}}
.links a{{color:var(--cyan);text-decoration:none;margin:0 2px}}
.links a:hover{{text-decoration:underline}}
footer{{color:var(--muted);font-size:.72rem;margin-top:8px}}
@media(max-width:600px){{ul.reports li{{flex-direction:column;align-items:flex-start}}.links{{margin-left:0}}}}
</style>
</head>
<body>
<main class="shell">
  <p class="eyebrow">All Screeners</p>
  <h1>Screener Reports</h1>
  <p class="meta">Every report this project generates, newest first. Regenerated automatically each time a screener runs.</p>
  {"".join(sections)}
  <footer>Index rebuilt {_h(generated_at)}.</footer>
</main>
</body>
</html>
"""


def write_index(directory: str | Path, *, generated_at: str | None = None) -> Path:
    """Rebuild ``index.html`` in ``directory`` from whatever reports exist there.

    Call this after any screener finishes writing its own report so the index
    never falls behind what is actually on disk.
    """
    stamp = generated_at or datetime.now(UTC).isoformat()
    entries = discover_reports(directory)
    page = render_index(entries, generated_at=stamp)
    target = Path(directory) / INDEX_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target
