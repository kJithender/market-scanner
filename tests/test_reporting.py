from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from market_scanner.reporting import (
    CSV_FIELDS,
    VOLATILITY_CSV_FIELDS,
    normalize_scan_result,
    render_csv,
    render_html,
    render_json,
    render_markdown,
    render_volatility_csv,
    write_reports,
)


@dataclass
class Candidate:
    ticker: str
    price: float
    volume: int
    relative_volume: float
    atr_pct: float
    beta: float
    spread_pct: float
    technical_structure: str
    key_levels: dict[str, float]
    catalyst: list[str]
    checklist: dict[str, str]


@dataclass
class Result:
    generated_at: datetime
    data_as_of: datetime
    watchlist: list[Candidate]
    warnings: list[str]


def sample_result(count: int = 2) -> Result:
    candidates = [
        Candidate(
            ticker=f"stk{index}",
            price=25.25 + index,
            volume=2_400_000 + index,
            relative_volume=2.1,
            atr_pct=3.4,
            beta=1.3,
            spread_pct=0.08,
            technical_structure="Higher highs & higher lows",
            key_levels={"support": 24.5, "resistance": 29.0},
            catalyst=["Earnings tomorrow", "Gap +3%"],
            checklist={
                "thesis": "Breakout above resistance",
                "stop": "$24.40",
                "target": "$31.00",
                "risk": "0.5% account risk",
            },
        )
        for index in range(1, count + 1)
    ]
    return Result(
        generated_at=datetime(2026, 8, 11, 13, tzinfo=UTC),
        data_as_of=datetime(2026, 8, 11, 12, 59, tzinfo=UTC),
        watchlist=candidates,
        warnings=["Premarket quotes can change quickly"],
    )


def test_normalize_accepts_dataclasses_and_caps_ranked_watchlist() -> None:
    report = normalize_scan_result(sample_result(17))

    assert report["status"] == "ok"
    assert report["candidate_count"] == 15
    assert [row["rank"] for row in report["candidates"]] == list(range(1, 16))
    assert report["candidates"][0]["symbol"] == "STK1"
    assert report["candidates"][0]["atr_percent"] == 3.4
    assert report["candidates"][0]["thesis"] == "Breakout above resistance"
    assert report["data_freshness"].startswith("Market data as of")


def test_machine_json_is_stable_and_valid() -> None:
    payload = json.loads(render_json(sample_result()))

    # 1.1 added volatility_candidates and volatility_count to the schema.
    assert payload["schema_version"] == "1.1"
    assert payload["timezone"] == "America/Los_Angeles"
    assert payload["candidate_count"] == 2
    assert payload["candidates"][0]["catalysts"] == ["Earnings tomorrow", "Gap +3%"]
    assert "Research only" in payload["disclaimer"]


def test_csv_has_expected_machine_columns_and_rows() -> None:
    rows = list(csv.DictReader(io.StringIO(render_csv(sample_result()))))

    assert tuple(rows[0]) == CSV_FIELDS
    assert len(rows) == 2
    assert rows[0]["symbol"] == "STK1"
    assert rows[0]["catalysts"] == "Earnings tomorrow; Gap +3%"
    assert rows[0]["risk"] == "0.5% account risk"


def test_markdown_includes_filters_metrics_and_checklist() -> None:
    output = render_markdown(sample_result())

    assert "# Morning Market Scanner" in output
    assert "## Applied filters" in output
    assert "| # | Symbol | Price | Volume | RVOL | ATR | Beta | Spread | Gap |" in output
    assert "**Thesis:** Breakout above resistance" in output
    assert "**RESEARCH" not in output  # Disclaimer remains readable prose, not an HTML token.
    assert "Research only" in output


def test_html_is_self_contained_responsive_and_escapes_untrusted_values() -> None:
    result = {
        "generated_at": "2026-08-11T06:00:00-07:00",
        "data_freshness": "Live-ish <verify>",
        "candidates": [
            {
                "symbol": "X<script>",
                "price": 12,
                "volume": 2_000_000,
                "rvol": 1.9,
                "atr_percent": 3,
                "beta": 1.2,
                "spread_percent": 0.1,
                "trend": "Clean",
                "levels": "10 / 14",
                "catalyst": "News",
                "thesis": "Momentum",
                "stop": "10",
                "target": "14",
                "risk": "1R",
            }
        ],
    }
    output = render_html(result)

    assert output.startswith("<!doctype html>")
    assert '<meta name="viewport"' in output
    assert "@media(max-width:680px)" in output
    assert "RESEARCH ONLY" in output
    assert "Applied scanner filters" in output
    assert "Pre-trade checklist" in output
    assert "X&lt;SCRIPT&gt;" in output
    assert "X<script>" not in output
    assert "https://" not in output


def test_empty_and_error_states_are_explicit_in_all_human_outputs() -> None:
    result = {"candidates": [], "error": "Provider timed out", "data_as_of": None}

    normalized = normalize_scan_result(result)
    assert normalized["status"] == "error"
    assert normalized["candidate_count"] == 0
    markdown = render_markdown(result)
    assert "Scanner error" in markdown
    assert "No stocks met every filter" in markdown
    html = render_html(result)
    assert "Scanner error" in html
    assert "No qualifying setups" in html
    assert "verify freshness" in html


def test_write_reports_creates_all_formats(tmp_path) -> None:
    paths = write_reports(sample_result(), tmp_path, basename="2026-08-11")

    assert set(paths) == {"json", "csv", "markdown", "html"}
    assert {path.suffix for path in paths.values()} == {".json", ".csv", ".md", ".html"}
    assert all(path.exists() and path.stat().st_size > 0 for path in paths.values())


@dataclass
class VolatileRow:
    symbol: str
    price: float
    avg_volume: float
    atr_percent: float
    rvol: float | None
    spread_percent: float | None
    gap_percent: float
    volume_confirmation: float | None
    trend: str
    volatility_score: float
    on_watchlist: bool = False


@dataclass
class ResultWithVolatility:
    generated_at: datetime
    data_as_of: datetime
    watchlist: list[Candidate]
    warnings: list[str]
    volatility_candidates: list[VolatileRow]


def sample_with_volatility(volatile: int = 3, watchlist: int = 2) -> ResultWithVolatility:
    base = sample_result(watchlist)
    return ResultWithVolatility(
        generated_at=base.generated_at,
        data_as_of=base.data_as_of,
        watchlist=base.watchlist,
        warnings=base.warnings,
        volatility_candidates=[
            VolatileRow(
                symbol=f"vol{index}",
                price=12.0 + index,
                avg_volume=3_000_000,
                atr_percent=9.0 - index,
                rvol=2.0,
                spread_percent=0.12,
                gap_percent=4.5,
                volume_confirmation=1.4,
                trend="uptrend",
                volatility_score=80.0 - index,
                on_watchlist=index == 1,
            )
            for index in range(1, volatile + 1)
        ],
    )


def test_volatility_rows_are_normalized_separately_from_the_watchlist() -> None:
    report = normalize_scan_result(sample_with_volatility())

    assert report["candidate_count"] == 2
    assert report["volatility_count"] == 3
    assert [row["rank"] for row in report["volatility_candidates"]] == [1, 2, 3]
    assert report["volatility_candidates"][0]["symbol"] == "VOL1"
    # No trade plan is invented for a name that never passed the gates.
    assert "thesis" not in report["volatility_candidates"][0]
    assert report["volatility_candidates"][0]["on_watchlist"] is True
    assert report["volatility_candidates"][1]["on_watchlist"] is False


def test_volatility_csv_is_its_own_file_with_its_own_columns() -> None:
    rows = list(csv.DictReader(io.StringIO(render_volatility_csv(sample_with_volatility()))))

    assert tuple(rows[0]) == VOLATILITY_CSV_FIELDS
    assert "thesis" not in rows[0]
    assert len(rows) == 3
    assert rows[0]["symbol"] == "VOL1"
    # _number narrows integral floats, so 8.0 is written as 8.
    assert rows[0]["atr_percent"] == "8"


def test_reports_state_that_volatility_names_are_not_gated() -> None:
    markdown = render_markdown(sample_with_volatility())
    html = render_html(sample_with_volatility())

    assert "## High-volatility list" in markdown
    assert "have not passed the hard" in markdown
    assert "High-volatility list" in html
    assert "Not a watchlist" in html


def test_volatility_section_is_omitted_when_the_list_is_empty() -> None:
    markdown = render_markdown(sample_result())

    assert "High-volatility list" not in markdown
    assert normalize_scan_result(sample_result())["volatility_count"] == 0


def test_volatility_csv_is_written_only_when_the_list_has_rows(tmp_path) -> None:
    paths = write_reports(sample_with_volatility(), tmp_path)
    volatility_path = paths["volatility_csv"]
    assert volatility_path.name == "market-scan-volatility.csv"
    assert "VOL1" in volatility_path.read_text(encoding="utf-8")

    # A later empty run must not leave yesterday's file looking like today's.
    paths = write_reports(sample_result(), tmp_path)
    assert "volatility_csv" not in paths
    assert not volatility_path.exists()
