"""Deterministic, dependency-free market indicators."""

from __future__ import annotations

from collections.abc import Sequence
from statistics import fmean

from market_scanner.models import Bar


def average_true_range(bars: Sequence[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        raise ValueError(f"ATR requires at least {period + 1} bars")
    ranges: list[float] = []
    for previous, current in zip(bars[-period - 1 : -1], bars[-period:], strict=True):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return fmean(ranges)


def atr_percent(bars: Sequence[Bar], price: float, period: int = 14) -> float:
    if price <= 0:
        raise ValueError("price must be positive")
    return average_true_range(bars, period) / price * 100


def return_beta(asset: Sequence[Bar], benchmark: Sequence[Bar], period: int = 60) -> float:
    asset_by_date = {bar.timestamp.date(): bar.close for bar in asset}
    benchmark_by_date = {bar.timestamp.date(): bar.close for bar in benchmark}
    dates = sorted(asset_by_date.keys() & benchmark_by_date.keys())[-(period + 1) :]
    if len(dates) < period + 1:
        raise ValueError(f"beta requires at least {period + 1} aligned bars")
    asset_returns = [
        asset_by_date[current] / asset_by_date[previous] - 1
        for previous, current in zip(dates[:-1], dates[1:], strict=True)
    ]
    market_returns = [
        benchmark_by_date[current] / benchmark_by_date[previous] - 1
        for previous, current in zip(dates[:-1], dates[1:], strict=True)
    ]
    market_mean = fmean(market_returns)
    asset_mean = fmean(asset_returns)
    covariance = fmean(
        (asset_return - asset_mean) * (market_return - market_mean)
        for asset_return, market_return in zip(asset_returns, market_returns, strict=True)
    )
    variance = fmean((market_return - market_mean) ** 2 for market_return in market_returns)
    if variance == 0:
        raise ValueError("benchmark variance is zero")
    return covariance / variance


def relative_volume(current: int, historical_same_window: Sequence[int]) -> float:
    usable = [value for value in historical_same_window if value > 0]
    if not usable:
        raise ValueError("RVOL requires prior same-window volume")
    return current / fmean(usable)


def spread_percent(bid: float, ask: float) -> float:
    if bid <= 0 or ask <= 0 or ask < bid:
        raise ValueError("quote must contain a valid positive bid/ask")
    midpoint = (bid + ask) / 2
    return (ask - bid) / midpoint * 100


def trend_structure(bars: Sequence[Bar]) -> tuple[bool, str, float]:
    """Quantify trend using MA alignment, 20-day slope, and path efficiency."""
    if len(bars) < 50:
        raise ValueError("trend requires at least 50 bars")
    closes = [bar.close for bar in bars]
    last = closes[-1]
    sma20 = fmean(closes[-20:])
    sma50 = fmean(closes[-50:])
    slope = (fmean(closes[-5:]) - fmean(closes[-20:-15])) / fmean(closes[-20:-15])
    travel = sum(
        abs(right - left) for left, right in zip(closes[-20:-1], closes[-19:], strict=True)
    )
    efficiency = abs(closes[-1] - closes[-20]) / travel if travel else 0.0
    up = last > sma20 > sma50 and slope > 0.01
    down = last < sma20 < sma50 and slope < -0.01
    clean = (up or down) and efficiency >= 0.25
    direction = "uptrend" if up else "downtrend" if down else "mixed"
    score = min(100.0, max(0.0, efficiency * 100 + min(abs(slope) * 500, 30)))
    return clean, direction, score


def volume_spike(bars: Sequence[Bar], lookback: int = 20) -> float:
    """Latest completed session's volume as a multiple of the prior average.

    The prior ``lookback`` sessions are averaged **excluding** the session
    being measured. Including it would put the spike inside its own baseline
    and systematically understate it — a genuine 10x day would read closer to
    7x once it had inflated the mean it is divided by.

    This is distinct from RVOL. RVOL compares the session so far against the
    same time of day in earlier sessions; this compares one finished session
    against recent finished sessions, so it is never diluted by how much of
    the trading day happens to have elapsed.
    """
    if lookback < 1:
        raise ValueError("volume lookback must be at least 1 session")
    if len(bars) < lookback + 1:
        raise ValueError(f"volume spike requires at least {lookback + 1} bars")
    baseline = fmean(bar.volume for bar in bars[-lookback - 1 : -1])
    if baseline <= 0:
        raise ValueError("volume baseline must be positive")
    return bars[-1].volume / baseline


def relative_strength_index(bars: Sequence[Bar], period: int = 14) -> float:
    """Wilder's RSI over ``period`` sessions, 0–100.

    Uses Wilder's smoothing (the original definition), not a simple mean of
    gains and losses: the simple-average variant is a different indicator and
    produces visibly different values on the same data.

    A stretch with no losing session has no finite RS, which is reported as
    100.0 rather than raising — that is the defined boundary of the measure,
    not missing data.
    """
    if period < 2:
        raise ValueError("RSI period must be at least 2")
    if len(bars) < period + 1:
        raise ValueError(f"RSI requires at least {period + 1} bars")
    closes = [bar.close for bar in bars]
    changes = [right - left for left, right in zip(closes[:-1], closes[1:], strict=True)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    average_gain = fmean(gains[:period])
    average_loss = fmean(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period

    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100 - 100 / (1 + relative_strength)


def momentum_percent(bars: Sequence[Bar], lookback: int) -> float:
    """Price change over ``lookback`` completed sessions, in percent.

    This is the only measure here that spans months rather than days. ATR,
    RVOL and the gap all describe today; a stock can be up 20% over a quarter
    while none of them register anything. Sustained relative strength is a
    separate question from today's volatility, so it gets its own metric.
    """
    if lookback < 1:
        raise ValueError("momentum lookback must be at least 1 session")
    if len(bars) < lookback + 1:
        raise ValueError(f"momentum requires at least {lookback + 1} bars")
    start = bars[-lookback - 1].close
    if start <= 0:
        raise ValueError("momentum requires a positive starting close")
    return (bars[-1].close / start - 1) * 100


def key_levels(bars: Sequence[Bar], lookback: int = 20) -> tuple[bool, float, float]:
    if len(bars) < lookback + 1:
        raise ValueError("levels require more history")
    window = bars[-lookback - 1 : -1]
    support = min(bar.low for bar in window)
    resistance = max(bar.high for bar in window)
    price = bars[-1].close
    clear = support < price and resistance > support and (resistance - support) / price >= 0.04
    return clear, support, resistance


def volume_trend_confirmation(bars: Sequence[Bar], lookback: int = 5) -> float:
    """Recent mean volume as a multiple of the equally long window before it.

    This is the volume half of a double confirmation: price says a move is
    happening, and this says whether participation is expanding while it does.
    A value above 1.0 means the recent window traded more than the window that
    preceded it; below 1.0 means the move is running on thinning volume.

    Distinct from both existing volume measures. ``relative_volume`` compares
    today's partial session against the same time of day historically, and
    ``volume_spike`` compares one finished session against a flat baseline.
    Neither spans a window, so neither can say whether participation is
    *trending*, which is the only form of the question a multi-session price
    move can be confirmed against.

    The two windows are adjacent and equally long so the ratio is not biased by
    window length, and the measured window is excluded from its own baseline
    for the same reason it is excluded in ``volume_spike``.
    """
    if lookback < 1:
        raise ValueError("volume confirmation lookback must be at least 1 session")
    if len(bars) < lookback * 2:
        raise ValueError(f"volume confirmation requires at least {lookback * 2} bars")
    recent = fmean(bar.volume for bar in bars[-lookback:])
    baseline = fmean(bar.volume for bar in bars[-lookback * 2 : -lookback])
    if baseline <= 0:
        raise ValueError("volume confirmation baseline must be positive")
    return recent / baseline
