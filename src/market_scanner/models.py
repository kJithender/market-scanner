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
    # Fundamental gates. Each is evaluated only when the filings supply the
    # metric; an unavailable metric leaves its gate unevaluated exactly as an
    # unavailable spread or RVOL does.
    #: Sustained relative strength: minimum price change over
    #: ``momentum_lookback_days`` completed sessions. 0.0 disables the gate.
    min_momentum_pct: float = 0.0
    momentum_lookback_days: int = 63
    #: Volume confirmation: recent mean volume over the equally long window
    #: before it. The second, independent leg of a double confirmation — price
    #: structure says a move is happening, this says participation is expanding
    #: while it happens. 0.0 disables the gate, which is the default so that
    #: enabling the measure cannot silently change an existing scan.
    #:
    #: The lookback is short on purpose. Confirming a 63-session momentum move
    #: with 63 sessions of volume answers a swing-horizon question; a scalper
    #: needs to know whether participation is expanding *now*, so the default
    #: compares the last week against the week before it.
    min_volume_confirmation: float = 0.0
    volume_confirmation_lookback_days: int = 5
    #: Reporting-only measures. They are surfaced on every row but gate
    #: nothing, so adding them cannot silently change which names qualify.
    volume_spike_lookback_days: int = 20
    rsi_period: int = 14
    use_fundamentals: bool = False
    min_revenue_growth_pct: float = 0.0
    max_debt_to_equity: float = 3.0
    require_profitable: bool = False
    #: When true, a symbol whose filings supply no fundamentals at all is
    #: rejected instead of being ranked on technicals alone.
    require_fundamentals: bool = False
    #: Reject filings whose newest period is older than this many days.
    max_fundamentals_age_days: int = 200
    #: Daily high-volatility list. Reported *beside* the watchlist, never
    #: inside it: these names clear a tradability floor and a volatility bar
    #: only, so folding them into the ranked watchlist would break the promise
    #: that every name there passed every hard gate.
    volatility_list_enabled: bool = True
    volatility_min_atr_pct: float = 5.0
    volatility_min_average_volume: int = 1_000_000
    #: Applied only when the provider publishes quotes. An unmeasurable spread
    #: leaves the check out rather than passing it, exactly as the hard gates do.
    volatility_max_spread_pct: float = 0.50
    #: Applied only when the provider publishes premarket volume. Scalping a
    #: volatile name with no flow behind it is the failure mode this prevents.
    volatility_min_rvol: float = 1.0
    volatility_list_size: int = 25

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
        if self.momentum_lookback_days < 1:
            raise ValueError("momentum_lookback_days must be at least 1")
        if self.volume_spike_lookback_days < 1:
            raise ValueError("volume_spike_lookback_days must be at least 1")
        if self.rsi_period < 2:
            raise ValueError("rsi_period must be at least 2")
        if self.volume_confirmation_lookback_days < 1:
            raise ValueError("volume_confirmation_lookback_days must be at least 1")
        if self.min_volume_confirmation < 0:
            raise ValueError("min_volume_confirmation cannot be negative")
        if self.min_momentum_pct and self.history_days <= self.momentum_lookback_days:
            raise ValueError("history_days must exceed momentum_lookback_days")
        if self.max_debt_to_equity <= 0:
            raise ValueError("max_debt_to_equity must be positive")
        if self.max_fundamentals_age_days < 1:
            raise ValueError("max_fundamentals_age_days must be at least 1")
        if self.require_fundamentals and not self.use_fundamentals:
            raise ValueError("require_fundamentals needs use_fundamentals enabled")
        if self.volatility_min_atr_pct < 0:
            raise ValueError("volatility_min_atr_pct cannot be negative")
        if self.volatility_max_spread_pct <= 0:
            raise ValueError("volatility_max_spread_pct must be positive")
        if self.volatility_min_rvol < 0:
            raise ValueError("volatility_min_rvol cannot be negative")
        if self.volatility_list_size < 1:
            raise ValueError("volatility_list_size must be at least 1")


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
    #: Price change over the configured momentum lookback, or ``None`` when
    #: history is too short to measure it.
    momentum_percent: float | None
    #: Latest completed session's volume over the prior 20-session average.
    #: Distinct from RVOL, which is a same-time-of-day intraday comparison.
    volume_spike: float | None
    #: Recent mean volume over the equally long window before it. Above 1.0
    #: means participation expanded while price moved — the volume half of a
    #: double confirmation. ``None`` when history is too short to measure it.
    volume_confirmation: float | None
    #: Wilder's 14-session RSI, 0–100.
    rsi: float | None
    #: Price over trailing-twelve-month diluted EPS from SEC filings. ``None``
    #: when earnings are negative or unfiled — a negative P/E is not a cheap
    #: one, so it is never rendered as a number.
    pe_ratio: float | None
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
    #: Filed fundamentals, or ``None`` when fundamentals were not requested.
    #: Individual metrics inside may still be ``None`` (see fundamentals.py).
    fundamentals: dict[str, Any] | None = None
    fundamental_score: float | None = None
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


@dataclass(frozen=True)
class VolatilityCandidate:
    """A high-volatility name for the daily volatility list.

    Deliberately *not* a :class:`Candidate`. It carries no thesis, stop, or
    target because it has not been through the hard gates that would justify
    one — it cleared a tradability floor and a volatility bar, nothing more.
    Giving it the same shape as a qualified candidate would invite exactly the
    confusion the separate list exists to prevent.
    """

    symbol: str
    price: float
    avg_volume: float
    atr_percent: float
    #: ``None`` whenever the provider publishes no evidence for the measure,
    #: never 0.0 — an unmeasured metric is not a measured absence.
    rvol: float | None
    spread_percent: float | None
    gap_percent: float
    volume_confirmation: float | None
    trend: str
    #: Ranking score for volatility alone; not comparable to ``Candidate.score``.
    volatility_score: float
    data_as_of: str
    #: True when this symbol also passed every hard gate and appears on the
    #: ranked watchlist. The lists overlap by design — a fully qualified name
    #: is not disqualified from being the day's most tradable range — and the
    #: flag makes the overlap visible instead of silently duplicating a row.
    on_watchlist: bool = False
    rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    #: High-volatility names, ranked separately. Populated every run whenever
    #: the list is enabled, including runs where no symbol passed every hard
    #: gate — an empty watchlist is the day this list is most useful.
    volatility_candidates: list[VolatilityCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "volatility_candidates": [
                candidate.to_dict() for candidate in self.volatility_candidates
            ],
        }
