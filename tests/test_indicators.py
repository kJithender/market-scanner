from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from market_scanner.indicators import (
    atr_percent,
    key_levels,
    momentum_percent,
    relative_strength_index,
    relative_volume,
    return_beta,
    spread_percent,
    trend_structure,
    volume_spike,
    volume_trend_confirmation,
)
from market_scanner.models import Bar


def bars_from_returns(returns: list[float], *, multiplier: float = 1.0) -> list[Bar]:
    price = 50.0
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result: list[Bar] = []
    for index, value in enumerate([0.0, *returns]):
        previous = price
        price *= 1 + value * multiplier
        result.append(
            Bar(
                start + timedelta(days=index),
                previous,
                price * 1.016,
                price * 0.984,
                price,
                2_000_000,
            )
        )
    return result


def test_beta_matches_known_linear_returns() -> None:
    returns = [0.004 + ((index % 7) - 3) * 0.003 for index in range(70)]
    benchmark = bars_from_returns(returns)
    asset = bars_from_returns(returns, multiplier=1.5)
    assert return_beta(asset, benchmark) == pytest.approx(1.5, abs=0.03)


def test_market_metrics_and_structure() -> None:
    returns = [0.004 + ((index % 5) - 2) * 0.001 for index in range(80)]
    bars = bars_from_returns(returns, multiplier=1.3)
    clean, direction, score = trend_structure(bars)
    levels, support, resistance = key_levels(bars)
    assert 2 <= atr_percent(bars, bars[-1].close) <= 5
    assert (clean, direction) == (True, "uptrend")
    assert score >= 25
    assert levels and support < resistance


def test_spread_and_time_adjusted_relative_volume() -> None:
    assert spread_percent(99.9, 100.1) == pytest.approx(0.2)
    assert relative_volume(300_000, [100_000, 200_000, 150_000]) == 2.0
    with pytest.raises(ValueError):
        relative_volume(1, [])


def test_momentum_measures_change_over_the_lookback_window() -> None:
    # 10 sessions of +10% compounding, measured across the last 5.
    bars = bars_from_returns([0.10] * 10)
    expected = (bars[-1].close / bars[-6].close - 1) * 100

    assert momentum_percent(bars, 5) == pytest.approx(expected)
    assert momentum_percent(bars, 5) == pytest.approx(61.051, abs=1e-3)


def test_momentum_is_negative_for_a_decline() -> None:
    bars = bars_from_returns([-0.05] * 8)
    assert momentum_percent(bars, 4) < 0


def test_momentum_rejects_windows_longer_than_the_history() -> None:
    bars = bars_from_returns([0.01] * 5)
    with pytest.raises(ValueError, match="momentum requires"):
        momentum_percent(bars, 50)
    with pytest.raises(ValueError, match="at least 1 session"):
        momentum_percent(bars, 0)


def bars_from_closes(closes: list[float], volumes: list[int] | None = None) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    sizes = volumes or [1_000_000] * len(closes)
    return [
        Bar(start + timedelta(days=index), close, close * 1.01, close * 0.99, close, sizes[index])
        for index, close in enumerate(closes)
    ]


def test_rsi_matches_a_hand_computed_wilder_value() -> None:
    # 14 changes: one -1 then thirteen +1, so the seeding window is exactly
    # full. average_gain = 13/14, average_loss = 1/14, RS = 13,
    # RSI = 100 - 100/14 = 92.857…
    closes = [100.0, 99.0] + [99.0 + step for step in range(1, 14)]
    assert len(closes) == 15

    assert relative_strength_index(bars_from_closes(closes), 14) == pytest.approx(92.857, abs=1e-3)


def test_rsi_saturates_and_centres_predictably() -> None:
    rising = bars_from_closes([100.0 + step for step in range(20)])
    falling = bars_from_closes([100.0 - step for step in range(20)])
    flat = bars_from_closes([100.0] * 20)

    assert relative_strength_index(rising, 14) == 100.0
    assert relative_strength_index(falling, 14) == 0.0
    # No movement at all is neither overbought nor oversold.
    assert relative_strength_index(flat, 14) == 50.0


def test_rsi_rejects_short_history_and_tiny_periods() -> None:
    with pytest.raises(ValueError, match="RSI requires"):
        relative_strength_index(bars_from_closes([100.0] * 5), 14)
    with pytest.raises(ValueError, match="period must be at least 2"):
        relative_strength_index(bars_from_closes([100.0] * 30), 1)


def test_volume_spike_excludes_the_measured_session_from_its_baseline() -> None:
    # Twenty sessions at 1M, then one at 10M. Dividing by a baseline that
    # included the spike would report ~7.3x instead of the true 10x.
    volumes = [1_000_000] * 20 + [10_000_000]
    bars = bars_from_closes([100.0] * 21, volumes)

    assert volume_spike(bars, 20) == pytest.approx(10.0)


def test_volume_spike_reports_quiet_sessions_below_one() -> None:
    volumes = [2_000_000] * 20 + [500_000]
    assert volume_spike(bars_from_closes([100.0] * 21, volumes), 20) == pytest.approx(0.25)


def test_volume_spike_requires_history_and_a_positive_baseline() -> None:
    with pytest.raises(ValueError, match="volume spike requires"):
        volume_spike(bars_from_closes([100.0] * 5), 20)
    with pytest.raises(ValueError, match="baseline must be positive"):
        volume_spike(bars_from_closes([100.0] * 21, [0] * 20 + [5_000_000]), 20)


def test_volume_confirmation_compares_adjacent_equal_windows() -> None:
    # Five quiet sessions, then five that trade twice as heavily.
    volumes = [1_000_000] * 5 + [2_000_000] * 5
    bars = bars_from_closes([100.0] * 10, volumes)

    assert volume_trend_confirmation(bars, 5) == pytest.approx(2.0)


def test_volume_confirmation_reports_a_fading_move_below_one() -> None:
    # Price can keep rising while participation drains away; that is precisely
    # the case the confirmation exists to expose, so it must read below 1.0.
    volumes = [4_000_000] * 5 + [1_000_000] * 5
    bars = bars_from_closes([100.0 + step for step in range(10)], volumes)

    assert volume_trend_confirmation(bars, 5) == pytest.approx(0.25)


def test_volume_confirmation_excludes_the_measured_window_from_its_baseline() -> None:
    # The two windows must not overlap. A baseline that included the recent
    # window would dilute a genuine 3x expansion toward 2x.
    volumes = [1_000_000] * 3 + [3_000_000] * 3
    assert volume_trend_confirmation(bars_from_closes([100.0] * 6, volumes), 3) == pytest.approx(
        3.0
    )


def test_volume_confirmation_requires_two_full_windows() -> None:
    with pytest.raises(ValueError, match="volume confirmation requires"):
        volume_trend_confirmation(bars_from_closes([100.0] * 9), 5)
    with pytest.raises(ValueError, match="lookback must be at least 1"):
        volume_trend_confirmation(bars_from_closes([100.0] * 20), 0)
    with pytest.raises(ValueError, match="baseline must be positive"):
        volume_trend_confirmation(
            bars_from_closes([100.0] * 6, [0] * 3 + [1_000_000] * 3), 3
        )
