"""Deterministic provider for setup validation, screenshots, and tests."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from market_scanner.models import Bar, Catalyst, MarketSnapshot, Quote, ScanConfig


class DemoProvider:
    name = "demo"

    async def get_snapshots(
        self, symbols: list[str], as_of: datetime, config: ScanConfig
    ) -> tuple[list[MarketSnapshot], list[str]]:
        benchmark = _bars("SPY", as_of, 96, beta_factor=1.0, base=55)
        snapshots: list[MarketSnapshot] = []
        for index, symbol in enumerate(symbols):
            base = 18 + index * 2.7
            if base > 140:
                base = 25 + index
            # Every fifth name trades a much wider range, so the demo produces
            # a non-empty high-volatility list as well as a full watchlist.
            # A fixture that only ever exercises one of the two output paths
            # cannot validate the pipeline the README says it validates.
            bars = _bars(
                symbol,
                as_of,
                96,
                beta_factor=1.25 + (index % 4) * 0.12,
                base=base,
                # Offset off index 0 so a single-symbol demo call still returns
                # the ordinary, fully qualifying name that tests rely on.
                range_factor=2.1 if index % 5 == 3 else 1.0,
            )
            previous = bars[-1].close
            price = previous * (1.022 + (index % 3) * 0.003)
            spread = price * (0.0008 + (index % 3) * 0.0002)
            historical = tuple(48_000 + day * 900 + index * 110 for day in range(12))
            current = int(sum(historical) / len(historical) * (1.72 + (index % 5) * 0.12))
            catalysts = (
                Catalyst(
                    "news",
                    f"Demo catalyst for {symbol} — replace with verified live headline",
                    as_of - timedelta(hours=2),
                ),
            )
            snapshots.append(
                MarketSnapshot(
                    symbol=symbol,
                    price=price,
                    quote=Quote(price - spread / 2, price + spread / 2, as_of),
                    daily_bars=tuple(bars),
                    benchmark_bars=tuple(benchmark),
                    current_premarket_volume=current,
                    historical_premarket_volumes=historical,
                    catalysts=catalysts,
                    data_as_of=as_of,
                )
            )
        return snapshots, [
            "DEMO DATA: deterministic synthetic prices, volume, quotes, and catalysts; "
            "do not trade."
        ]


def _bars(
    symbol: str,
    as_of: datetime,
    count: int,
    *,
    beta_factor: float,
    base: float,
    range_factor: float = 1.0,
) -> list[Bar]:
    del symbol
    start = as_of.astimezone(UTC).date() - timedelta(days=count * 2)
    bars: list[Bar] = []
    price = base
    market_price = base
    day = start
    index = 0
    while len(bars) < count:
        if day.weekday() < 5:
            market_return = 0.003 + math.sin(index * 0.73) * 0.009
            asset_return = 0.0035 + beta_factor * (market_return - 0.003)
            market_price *= 1 + market_return
            price *= 1 + asset_return
            # Range ~3% while closes remain directionally efficient, widened
            # by ``range_factor`` for the deliberately volatile names.
            high = price * (1 + 0.016 * range_factor)
            low = price * (1 - 0.016 * range_factor)
            bars.append(
                Bar(
                    datetime.combine(day, datetime.min.time(), UTC),
                    price / (1 + asset_return),
                    high,
                    low,
                    price,
                    1_600_000 + (index % 7) * 90_000,
                )
            )
            index += 1
        day += timedelta(days=1)
    # The newest sessions trade heavier than the ones before them. The base
    # volume above is a 7-session sawtooth that mean-reverts, so any window
    # ratio sits at ~1.0 and the fixture could never clear a volume
    # confirmation threshold — not because the pipeline is wrong, but because
    # synthetic volume that never trends has nothing to confirm.
    expansion = 5
    bars[-expansion:] = [
        replace(bar, volume=int(bar.volume * 1.35)) for bar in bars[-expansion:]
    ]
    return bars
