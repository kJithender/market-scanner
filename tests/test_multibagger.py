"""Multibagger measurement and reporting. Deterministic; no network access."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from market_scanner.models import Bar
from market_scanner.multibagger import (
    SESSIONS_PER_YEAR,
    MultibaggerConfig,
    build_result,
    evaluate,
    price_multiple,
    rank_multibaggers,
)
from market_scanner.multibagger_reporting import render_csv, render_html, render_markdown


def bars(closes: list[float], volume: int = 5_000_000) -> list[Bar]:
    start = datetime(2021, 1, 1, tzinfo=UTC)
    return [
        Bar(start + timedelta(days=index), close, close * 1.01, close * 0.99, close, volume)
        for index, close in enumerate(closes)
    ]


def ramp(multiple: float, sessions: int, *, base: float = 10.0) -> list[float]:
    """Closes rising smoothly to ``multiple`` times ``base`` over ``sessions``."""
    return [base * (1 + (multiple - 1) * index / sessions) for index in range(sessions + 1)]


def test_price_multiple_measures_the_requested_window() -> None:
    closes = [10.0, 15.0, 20.0, 30.0]
    assert price_multiple(closes, 3) == pytest.approx(3.0)
    assert price_multiple(closes, 1) == pytest.approx(1.5)


def test_price_multiple_is_none_when_history_is_too_short() -> None:
    # A recent listing must never be reported as having a long-horizon record.
    assert price_multiple([10.0, 20.0], 5) is None
    assert price_multiple([10.0, 20.0], 0) is None


def test_evaluate_reports_multiples_per_horizon() -> None:
    config = MultibaggerConfig(horizons_years=(1.0, 2.0), history_years=5)
    result = evaluate("TEST", bars(ramp(4.0, 2 * SESSIONS_PER_YEAR)), config)

    assert result is not None
    assert set(result.multiples) == {"1y", "2y"}
    assert result.multiples["2y"] == pytest.approx(4.0, rel=1e-3)
    # A steady ramp gains less over the recent year than the full two.
    assert result.multiples["1y"] < result.multiples["2y"]
    assert result.best_multiple == pytest.approx(4.0, rel=1e-3)


def test_evaluate_rejects_names_below_the_multiple() -> None:
    config = MultibaggerConfig(min_multiple=3.0, horizons_years=(1.0,), history_years=5)
    assert evaluate("TEST", bars(ramp(1.5, SESSIONS_PER_YEAR)), config) is None


def test_evaluate_enforces_liquidity_and_price_floors() -> None:
    config = MultibaggerConfig(horizons_years=(1.0,), history_years=5)
    illiquid = bars(ramp(5.0, SESSIONS_PER_YEAR), volume=1_000)
    assert evaluate("TEST", illiquid, config) is None

    penny = bars([0.10 + 0.001 * i for i in range(SESSIONS_PER_YEAR + 1)])
    assert evaluate("TEST", penny, MultibaggerConfig(min_price=5.0, horizons_years=(1.0,))) is None


def test_a_past_multibagger_that_is_now_falling_is_flagged() -> None:
    """The report's main way of misleading would be showing a 10x without
    saying the stock has since halved."""
    config = MultibaggerConfig(horizons_years=(1.0, 2.0), history_years=5)
    closes = ramp(10.0, SESSIONS_PER_YEAR)  # year one: up 10x
    closes += [closes[-1] * (1 - 0.5 * i / SESSIONS_PER_YEAR) for i in range(SESSIONS_PER_YEAR)]

    result = evaluate("TEST", bars(closes), config)

    assert result is not None
    assert result.best_multiple > 2.0
    assert result.recent_multiple is not None and result.recent_multiple < 1.0
    assert result.to_dict()["still_rising"] is False


def test_ranking_is_strongest_first_and_capped_without_padding() -> None:
    config = MultibaggerConfig(horizons_years=(1.0,), history_years=5, max_results=2)
    found = [
        evaluate(symbol, bars(ramp(multiple, SESSIONS_PER_YEAR)), config)
        for symbol, multiple in (("LOW", 2.5), ("HIGH", 9.0), ("MID", 4.0))
    ]
    ranked = rank_multibaggers([item for item in found if item], config)

    assert [item.symbol for item in ranked] == ["HIGH", "MID"]
    assert [item.rank for item in ranked] == [1, 2]


def test_build_result_reports_symbols_without_history() -> None:
    config = MultibaggerConfig(horizons_years=(1.0,), history_years=5)
    history = {"GOOD": bars(ramp(5.0, SESSIONS_PER_YEAR))}
    result = build_result(
        history=history,
        symbols=["GOOD", "MISSING"],
        config=config,
        provider="test",
        generated_at="2026-08-18T00:00:00+00:00",
    )

    assert result.symbols_scanned == 2
    assert result.symbols_qualified == 1
    assert any("MISSING" in warning for warning in result.warnings)


def test_config_rejects_history_shorter_than_the_deepest_horizon() -> None:
    with pytest.raises(ValueError, match="history_years must cover"):
        MultibaggerConfig(horizons_years=(1.0, 5.0), history_years=3)


def _sample_result() -> object:
    config = MultibaggerConfig(horizons_years=(1.0, 2.0), history_years=5)
    history = {"AAA": bars(ramp(6.0, 2 * SESSIONS_PER_YEAR))}
    return build_result(
        history=history,
        symbols=["AAA"],
        config=config,
        provider="test",
        generated_at="2026-08-18T00:00:00+00:00",
    )


def test_renderers_state_the_report_is_historical() -> None:
    result = _sample_result()
    markdown, page = render_markdown(result), render_html(result)

    for text in (markdown, page):
        assert "not a forecast" in text
        assert "AAA" in text
    assert "ALREADY HAPPENED" in page


def test_csv_has_one_column_per_horizon() -> None:
    rows = render_csv(_sample_result()).splitlines()
    header = rows[0].split(",")

    assert "multiple_1y" in header
    assert "multiple_2y" in header
    assert "still_rising" in header
    assert rows[1].startswith("1,AAA,")
