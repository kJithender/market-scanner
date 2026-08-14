"""Domain models shared by providers, scanner logic, and reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ScanConfig:
    min_price: float = 5.0
    max_price: float = 150.0
    min_average_volume: int = 1_000_000
    max_spread_pct: float = 0.30
    min_atr_pct: float = 2.0
    max_atr_pct: float = 5.0
    min_beta: float = 1.0
    min_rvol: float = 1.5
    min_gap_pct: float = 2.0
    watchlist_size: int = 15
    minimum_watchlist_size: int = 10
    history_days: int = 90
    level_lookback_days: int = 20
    risk_per_trade_dollars: float = 100.0
    reward_to_risk: float = 2.0
    require_catalyst: bool = True

    def __post_init__(self) -> None:
        if self.min_price > self.max_price:
            raise ValueError("min_price cannot exceed max_price")
        if self.min_atr_pct > self.max_atr_pct:
            raise ValueError("min_atr_pct cannot exceed max_atr_pct")
        if self.min_gap_pct < 0:
            raise ValueError("min_gap_pct cannot be negative")
        if self.watchlist_size < 1:
            raise ValueError("watchlist_size must be at least 1")
        if not 1 <= self.minimum_watchlist_size <= self.watchlist_size:
            raise ValueError("minimum_watchlist_size must be <= watchlist_size")
        if self.history_days < 60:
            raise ValueError("history_days must be at least 60 for beta")
        if self.level_lookback_days < 5:
            raise ValueError("level_lookback_days must be at least 5")
        if self.risk_per_trade_dollars <= 0 or self.reward_to_risk <= 0:
            raise ValueError("risk controls must be positive")


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    timestamp: datetime


@dataclass(frozen=True)
class Catalyst:
    kind: str
    description: str
    timestamp: datetime | None = None
    url: str | None = None


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    price: float
    quote: Quote
    daily_bars: tuple[Bar, ...]
    benchmark_bars: tuple[Bar, ...]
    current_premarket_volume: int
    historical_premarket_volumes: tuple[int, ...]
    catalysts: tuple[Catalyst, ...] = ()
    data_as_of: datetime | None = None
    rvol_method: str = "same-window premarket volume / prior-session mean"
    #: Gate names the provider cannot supply evidence for. Such gates are left
    #: unevaluated instead of being scored as a pass.
    unverified_gates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    symbol: str
    price: float
    avg_volume: float
    current_volume: int
    rvol: float | None
    rvol_method: str
    atr_percent: float
    beta: float
    spread_percent: float | None
    gap_percent: float
    trend: str
    trend_score: float
    levels: dict[str, float]
    catalysts: list[str]
    catalyst_details: list[dict[str, Any]]
    thesis: str
    stop: float
    target: float
    risk: str
    score: float
    data_as_of: str
    passed_filters: dict[str, bool]
    rank: int = 0

    @property
    def pre_trade_checklist(self) -> dict[str, Any]:
        return {
            "thesis": self.thesis,
            "stop": self.stop,
            "target": self.target,
            "risk": self.risk,
        }

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["pre_trade_checklist"] = self.pre_trade_checklist
        return result


@dataclass
class ScanResult:
    candidates: list[Candidate]
    generated_at: str
    data_as_of: str | None
    provider: str
    symbols_scanned: int
    symbols_qualified: int
    filters: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rejection_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }
