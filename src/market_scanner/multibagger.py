"""Historical multibagger report: which names have already multiplied.

Deliberately separate from the premarket scanner, for two reasons.

*Horizon.* The scanner gates on RVOL, ATR and today's gap — every one a
single-session measure. A multibagger is a multi-year return, so it needs a
different data window entirely and answers a different question.

*Contract.* The scanner's guarantee is that every listed name cleared every
hard gate. Folding multibaggers in would need an OR-path that admits names
which never passed RVOL or the gap test, which would quietly make that
guarantee false. A separate report keeps it true.

Nothing here is predictive. A multiple is what a stock **has** done; the
report exists to say that plainly, including when a former multibagger is now
falling — which is why the one-year column sits alongside the longer ones.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any

from market_scanner.models import Bar

#: Trading sessions in a calendar year, the unit every horizon is expressed in.
SESSIONS_PER_YEAR = 252


@dataclass(frozen=True)
class MultibaggerConfig:
    #: Minimum price multiple over any measured horizon. 2.0 == has doubled.
    min_multiple: float = 2.0
    #: Liquidity floor, so results are tradeable rather than microcap artifacts.
    min_average_volume: int = 1_000_000
    min_price: float = 1.0
    #: Horizons to measure, in years.
    horizons_years: tuple[float, ...] = (1.0, 2.0, 3.0)
    #: Daily history to request. Yahoo returns slightly fewer sessions than a
    #: range implies, so asking for exactly the deepest horizon leaves it
    #: unmeasurable; request more than you intend to measure.
    history_years: int = 5
    max_results: int = 100

    def __post_init__(self) -> None:
        if self.min_multiple <= 0:
            raise ValueError("min_multiple must be positive")
        if not self.horizons_years:
            raise ValueError("at least one horizon is required")
        if any(horizon <= 0 for horizon in self.horizons_years):
            raise ValueError("horizons must be positive")
        if self.history_years < max(self.horizons_years):
            raise ValueError("history_years must cover the deepest horizon")
        if self.max_results < 1:
            raise ValueError("max_results must be at least 1")


@dataclass(frozen=True)
class Multibagger:
    symbol: str
    price: float
    average_volume: float
    #: Horizon label ("1y", "2y", …) to price multiple. A horizon the listing
    #: is too young to cover is absent rather than zero.
    multiples: dict[str, float]
    #: Multiple over the entire available history, whatever its length.
    max_multiple: float
    #: Multiple measured from the lowest close in the available history.
    off_low_multiple: float
    history_years: float
    best_horizon: str
    best_multiple: float
    #: Shortest measured horizon, surfaced so a stale winner is visible.
    recent_multiple: float | None = None
    rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "price": round(self.price, 4),
            "average_volume": round(self.average_volume, 0),
            "multiples": {key: round(value, 3) for key, value in self.multiples.items()},
            "max_multiple": round(self.max_multiple, 3),
            "off_low_multiple": round(self.off_low_multiple, 3),
            "history_years": self.history_years,
            "best_horizon": self.best_horizon,
            "best_multiple": round(self.best_multiple, 3),
            "recent_multiple": (
                None if self.recent_multiple is None else round(self.recent_multiple, 3)
            ),
            "still_rising": None if self.recent_multiple is None else self.recent_multiple >= 1.0,
        }


@dataclass
class MultibaggerResult:
    results: list[Multibagger]
    generated_at: str
    provider: str
    symbols_scanned: int
    symbols_qualified: int
    criteria: dict[str, str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "provider": self.provider,
            "symbols_scanned": self.symbols_scanned,
            "symbols_qualified": self.symbols_qualified,
            "criteria": self.criteria,
            "warnings": self.warnings,
            "results": [item.to_dict() for item in self.results],
            "disclaimer": DISCLAIMER,
        }


DISCLAIMER = (
    "Historical returns only — not investment advice and not a forecast. Every multiple "
    "below has already happened; it says nothing about future direction. Check the 1y "
    "column: several past multibaggers are currently falling."
)


def horizon_label(years: float) -> str:
    return f"{years:g}y"


def price_multiple(closes: Sequence[float], sessions: int) -> float | None:
    """Price multiple over ``sessions`` completed sessions, or ``None``.

    Returns ``None`` when the listing is too young to cover the window, so a
    recent IPO is never reported as though it had a three-year record.
    """
    if sessions < 1 or len(closes) < sessions + 1:
        return None
    start = closes[-sessions - 1]
    if start <= 0:
        return None
    return closes[-1] / start


def evaluate(symbol: str, bars: Sequence[Bar], config: MultibaggerConfig) -> Multibagger | None:
    """Measure one symbol, or return ``None`` when it does not qualify."""
    closes = [bar.close for bar in bars if bar.close > 0]
    if len(closes) < 2:
        return None
    price = closes[-1]
    if price < config.min_price:
        return None
    volumes = [bar.volume for bar in bars[-20:] if bar.volume > 0]
    average_volume = fmean(volumes) if volumes else 0.0
    if average_volume < config.min_average_volume:
        return None

    multiples: dict[str, float] = {}
    for years in sorted(config.horizons_years):
        value = price_multiple(closes, int(round(years * SESSIONS_PER_YEAR)))
        if value is not None:
            multiples[horizon_label(years)] = value
    max_multiple = price / closes[0] if closes[0] > 0 else 0.0
    if not multiples and max_multiple <= 0:
        return None

    best_multiple = max([*multiples.values(), max_multiple])
    if best_multiple < config.min_multiple:
        return None
    best_horizon = next(
        (label for label, value in multiples.items() if value == best_multiple), "max"
    )
    low = min(closes)
    shortest = horizon_label(min(config.horizons_years))
    return Multibagger(
        symbol=symbol,
        price=price,
        average_volume=average_volume,
        multiples=multiples,
        max_multiple=max_multiple,
        off_low_multiple=price / low if low > 0 else 0.0,
        history_years=round(len(closes) / SESSIONS_PER_YEAR, 1),
        best_horizon=best_horizon,
        best_multiple=best_multiple,
        recent_multiple=multiples.get(shortest),
    )


def criteria_descriptions(config: MultibaggerConfig) -> dict[str, str]:
    horizons = ", ".join(horizon_label(years) for years in sorted(config.horizons_years))
    return {
        "multiple": f"≥{config.min_multiple:g}x over any measured horizon",
        "horizons": f"{horizons} (plus full available history)",
        "liquidity": f">{config.min_average_volume:,} shares (20-session mean)",
        "price": f"≥${config.min_price:g}",
        "basis": "split/dividend-adjusted daily closes",
        "nature": "historical returns; not a forecast and not a watchlist",
    }


def rank_multibaggers(
    found: Sequence[Multibagger], config: MultibaggerConfig
) -> list[Multibagger]:
    """Rank by best multiple, strongest first, then cap. Never padded."""
    ordered = sorted(found, key=lambda item: (-item.best_multiple, item.symbol))
    capped = ordered[: config.max_results]
    return [
        Multibagger(**{**item.__dict__, "rank": position})
        for position, item in enumerate(capped, start=1)
    ]


def build_result(
    *,
    history: Mapping[str, Sequence[Bar]],
    symbols: Sequence[str],
    config: MultibaggerConfig,
    provider: str,
    generated_at: str,
    warnings: Sequence[str] = (),
) -> MultibaggerResult:
    found = [
        outcome
        for symbol in symbols
        if (bars := history.get(symbol)) and (outcome := evaluate(symbol, bars, config))
    ]
    ranked = rank_multibaggers(found, config)
    notes = list(warnings)
    missing = [symbol for symbol in symbols if not history.get(symbol)]
    if missing:
        notes.append(
            f"No usable history for {len(missing)} symbols: {', '.join(missing[:10])}"
            + ("…" if len(missing) > 10 else "")
        )
    return MultibaggerResult(
        results=ranked,
        generated_at=generated_at,
        provider=provider,
        symbols_scanned=len(symbols),
        symbols_qualified=len(ranked),
        criteria=criteria_descriptions(config),
        warnings=notes,
    )
