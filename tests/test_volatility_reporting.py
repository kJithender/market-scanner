from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from market_scanner.volatility_reporting import (
    BASENAME,
    DISCLAIMER,
    render_csv,
    render_html,
    render_json,
    render_markdown,
    write_reports,
)


def _result(candidates: list[dict] | None = None) -> dict:
    return {
        "generated_at": "2026-08-24T00:00:00+00:00",
        "data_as_of": "2026-08-24T00:00:00+00:00",
        "provider": "demo",
        "volatility_candidates": candidates or [],
        "warnings": [],
    }


def _row(**overrides) -> dict:
    row = {
        "rank": 1,
        "symbol": "VOLA",
        "price": 12.5,
        "atr_percent": 7.2,
        "rvol": 3.1,
        "spread_percent": 0.4,
        "gap_percent": 5.0,
        "volume_confirmation": 1.8,
        "trend": "uptrend",
        "volatility_score": 91.2,
        "on_watchlist": True,
    }
    row.update(overrides)
    return row


def test_render_json_carries_the_candidates_and_the_disclaimer() -> None:
    parsed = json.loads(render_json(_result([_row(symbol="VOLA")])))
    assert parsed["volatility_count"] == 1
    assert parsed["candidates"][0]["symbol"] == "VOLA"
    assert parsed["disclaimer"] == DISCLAIMER


def test_render_csv_matches_the_shared_volatility_columns() -> None:
    text = render_csv(_result([_row(symbol="VOLA")]))
    assert text.splitlines()[0].split(",")[:2] == ["rank", "symbol"]
    assert "VOLA" in text


def test_render_markdown_states_the_disclaimer_and_lists_rows() -> None:
    markdown = render_markdown(_result([_row(symbol="VOLA")]))
    assert DISCLAIMER in markdown
    assert "VOLA" in markdown
    assert "# High-volatility report" in markdown


def test_render_markdown_is_honest_when_nothing_qualifies() -> None:
    markdown = render_markdown(_result([]))
    assert "No high-volatility names on this run" in markdown
    assert "not padded" in markdown


def test_render_html_shows_rows_and_the_not_gated_banner() -> None:
    page = render_html(_result([_row(symbol="VOLA")]))
    assert "VOLA" in page
    assert "NOT GATED" in page
    assert DISCLAIMER in page


def test_render_html_is_honest_when_nothing_qualifies() -> None:
    page = render_html(_result([]))
    assert "No high-volatility names" in page
    assert "VOLA" not in page


def test_write_reports_always_writes_all_four_formats_even_when_empty(
    tmp_path: Path,
) -> None:
    """The regression this guards: the old CSV-only version deleted its file
    when the list was empty, so the report would silently disappear on a
    quiet day. This report must exist on every run instead."""
    written = write_reports(_result([]), tmp_path, run_date=date(2026, 8, 24))

    assert set(written) == {"json", "csv", "markdown", "html"}
    for path in written.values():
        assert path.exists()
        assert path.stat().st_size > 0


def test_write_reports_uses_the_exact_requested_filename_pattern(tmp_path: Path) -> None:
    written = write_reports(_result([_row()]), tmp_path, run_date=date(2026, 8, 24))

    assert written["html"].name == f"{BASENAME}-24-08-2026.html"
    assert written["json"].name == f"{BASENAME}-24-08-2026.json"
    assert written["csv"].name == f"{BASENAME}-24-08-2026.csv"
    assert written["markdown"].name == f"{BASENAME}-24-08-2026.md"
    assert BASENAME == "High-volatility"


def test_a_rerun_on_the_same_day_overwrites_rather_than_accumulating(
    tmp_path: Path,
) -> None:
    write_reports(_result([_row(symbol="FIRST")]), tmp_path, run_date=date(2026, 8, 24))
    write_reports(_result([_row(symbol="SECOND")]), tmp_path, run_date=date(2026, 8, 24))

    files = list(tmp_path.glob(f"{BASENAME}-24-08-2026.*"))
    assert len(files) == 4
    html_path = next(path for path in files if path.suffix == ".html")
    text = html_path.read_text(encoding="utf-8")
    assert "SECOND" in text
    assert "FIRST" not in text
