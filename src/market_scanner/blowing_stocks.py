"""BlowingStocksScreener — low-float momentum and catalyst-breakout screens.

Deliberately separate from ``scanner`` for the same reason ``multibagger`` is.

*Different universe.* The scanner screens 245 curated large/mid caps for a
clean, liquid day-trade setup. This screener looks for the opposite kind of
name: a sub-$20, sub-20-million-share float running 5x its normal volume on a
headline. Those names are not on a curated list — they are whichever twenty of
seven thousand tickers happen to be blowing up this morning — so the universe
is discovered each run rather than maintained.

*Different contract.* The scanner promises every listed name cleared every
hard gate. Here the defining gate is **float**, which no free price feed
publishes. Rather than quietly dropping the gate, float is reconstructed from
SEC cover-page filings (see ``float_data``) and a name whose float cannot be
proven is rejected, not passed. ``require_float = false`` relaxes that, and
the report always says which it was.

Two screens run side by side and are reported separately, because they select
for different things and a name can legitimately appear on one, the other, or
both:

``low_float``
    Screen A. Price, float, RVOL, gap, volume, news, and a breakout over both
    the premarket high and VWAP.

``catalyst``
    Screen B. A wider price band, a volume surge against the 20-session mean,
    a keyword-matched catalyst, and a breakout over both yesterday's high and
    the 50-session SMA.

Nothing here is a recommendation. Every number is a measurement of what has
already printed this morning.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

# --------------------------------------------------------------- keywords

#: Headline patterns for the catalysts the brief names. Multi-form on purpose:
#: a wire headline says "beats estimates" far more often than "earnings beat",
#: and a plain substring search for "AI" matches "said", "maintain" and "Air" —
#: which is why that one alternative is matched case-sensitively.
KEYWORD_PATTERNS: dict[str, str] = {
    "fda": r"\bFDA\b|\bFood and Drug Administration\b",
    "patent": r"\bpatents?\b|\bpatented\b|\bUSPTO\b",
    "reverse split": r"\breverse[\s-]?(?:stock\s+)?split\b",
    "ai": r"(?-i:\bAI\b)|\bartificial intelligence\b|\bmachine learning\b",
    "partnership": (
        r"\bpartnership\b|\bpartners?\s+with\b|\bstrategic\s+partner\w*\b"
        r"|\bcollaborat\w+\s+with\b|\bjoint\s+venture\b"
    ),
    "earnings beat": (
        r"\bearnings\s+beat\b|\bEPS\s+beat\b"
        r"|\bbeats?\s+(?:on\s+)?(?:earnings|estimates|expectations|consensus)\b"
        r"|\btops?\s+(?:estimates|expectations|consensus)\b"
    ),
    "phase 2": r"\bphase\s*(?:2|ii)\b",
    "phase 3": r"\bphase\s*(?:3|iii)\b",
    "guidance raise": (
        r"\brais\w*\s+(?:full[\s-]year\s+)?(?:guidance|outlook|forecast)\b"
        r"|\bguidance\s+rais\w+\b|\bboosts?\s+(?:its\s+)?(?:guidance|outlook|forecast)\b"
    ),
}


def keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Compile one keyword into a headline matcher.

    A keyword with a known multi-form pattern uses it; anything else becomes a
    word-boundary match with flexible internal whitespace, so a configured
    "share buyback" still matches "share  buyback" across a line break.
    """
    known = KEYWORD_PATTERNS.get(keyword.strip().lower())
    if known:
        return re.compile(known, re.IGNORECASE)
    parts = [re.escape(part) for part in keyword.strip().split()]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


def matched_keywords(headlines: Sequence[str], keywords: Sequence[str]) -> list[str]:
    """Keywords whose pattern appears in at least one headline, in config order."""
    text = " || ".join(headlines)
    if not text:
        return []
    return [keyword for keyword in keywords if keyword_pattern(keyword).search(text)]


# ----------------------------------------------------------------- config

DEFAULT_LOW_FLOAT_KEYWORDS = (
    "FDA",
    "Patent",
    "Reverse split",
    "AI",
    "Partnership",
    "Earnings beat",
)
DEFAULT_CATALYST_KEYWORDS = (
    "FDA",
    "Phase 2",
    "Phase 3",
    "Patent",
    "AI",
    "Partnership",
    "Earnings beat",
    "Guidance raise",
)

#: Tier labels, weakest first. A screen reports the tier each dimension
#: reached plus an overall tier that is the weakest of them, so "parabolic"
#: never describes a name that is parabolic on one axis alone.
TIERS = ("standard", "explosive", "parabolic")


@dataclass(frozen=True)
class LowFloatCriteria:
    """Screen A — Low-Float Momentum Explosion.

    ``min_*``/``max_*`` are the hard gates. ``best_*`` and ``parabolic_*`` gate
    nothing; they mark where a name stops being merely qualified and starts
    being the kind of setup the screen exists to catch.
    """

    enabled: bool = True
    min_price: float = 0.50
    max_price: float = 20.0
    max_float: float = 20_000_000
    best_float: float = 10_000_000
    parabolic_float: float = 5_000_000
    min_rvol: float = 5.0
    explosive_rvol: float = 10.0
    parabolic_rvol: float = 20.0
    min_gap_pct: float = 20.0
    best_gap_pct: float = 30.0
    min_volume: int = 500_000
    #: Applied instead of ``min_volume`` before 09:30 ET, where a full
    #: session's volume cannot exist yet.
    min_premarket_volume: int = 200_000
    require_news: bool = True
    keywords: tuple[str, ...] = DEFAULT_LOW_FLOAT_KEYWORDS
    #: "Has News" is the brief's gate; the keywords rank and label it. Set true
    #: to make a keyword match mandatory as well.
    require_keyword: bool = False
    require_above_premarket_high: bool = True
    require_above_vwap: bool = True

    def __post_init__(self) -> None:
        if self.min_price <= 0 or self.min_price >= self.max_price:
            raise ValueError("low_float price band must be positive and ordered")
        if not 0 < self.parabolic_float <= self.best_float <= self.max_float:
            raise ValueError("float tiers must be positive and ordered tightest-first")
        if not 0 < self.min_rvol <= self.explosive_rvol <= self.parabolic_rvol:
            raise ValueError("rvol tiers must be positive and ordered lowest-first")
        if self.min_gap_pct < 0 or self.best_gap_pct < self.min_gap_pct:
            raise ValueError("gap tiers must be non-negative and ordered")
        if self.min_volume < 0 or self.min_premarket_volume < 0:
            raise ValueError("volume floors cannot be negative")
        if self.require_keyword and not self.keywords:
            raise ValueError("require_keyword needs at least one keyword")


@dataclass(frozen=True)
class CatalystCriteria:
    """Screen B — Catalyst + Volume Breakout (biotech, AI, earnings)."""

    enabled: bool = True
    min_price: float = 1.0
    max_price: float = 50.0
    min_rvol: float = 3.0
    #: Today's cumulative volume as a multiple of the 20-session mean.
    min_volume_multiple: float = 2.0
    volume_average_days: int = 20
    keywords: tuple[str, ...] = DEFAULT_CATALYST_KEYWORDS
    #: True by default: this screen is *defined* by its catalyst, so news with
    #: no keyword match is not the thing it is looking for.
    require_keyword: bool = True
    require_above_previous_high: bool = True
    require_above_sma: bool = True
    sma_period: int = 50

    def __post_init__(self) -> None:
        if self.min_price <= 0 or self.min_price >= self.max_price:
            raise ValueError("catalyst price band must be positive and ordered")
        if self.min_rvol < 0:
            raise ValueError("catalyst min_rvol cannot be negative")
        if self.min_volume_multiple <= 0:
            raise ValueError("min_volume_multiple must be positive")
        if self.volume_average_days < 2:
            raise ValueError("volume_average_days must be at least 2")
        if self.sma_period < 2:
            raise ValueError("sma_period must be at least 2")
        if self.require_keyword and not self.keywords:
            raise ValueError("require_keyword needs at least one keyword")


@dataclass(frozen=True)
class BlowingStocksConfig:
    """Everything the screener needs that is not one screen's threshold."""

    low_float: LowFloatCriteria = field(default_factory=LowFloatCriteria)
    catalyst: CatalystCriteria = field(default_factory=CatalystCriteria)
    #: Rows kept per screen. A ceiling, never a target: the list is not padded.
    max_results: int = 50
    #: Days of dated reports kept under ``history/``. Older ones are deleted.
    history_retention_days: int = 7
    #: A float no source could prove rejects the symbol. Turning this off keeps
    #: the name and records the gate as unproven instead — which makes "low
    #: float" an unverified claim on those rows, so the report says so loudly.
    require_float: bool = True
    #: Reject a filed float whose measurement date is older than this. Float
    #: moves on offerings and lockup expiries; a three-year-old cover page is
    #: not evidence about this morning.
    max_float_age_days: int = 400
    #: Sessions of completed daily history loaded per surviving symbol. Must
    #: cover the 50-session SMA with room for holidays.
    history_days: int = 90
    #: Prior sessions used for the same-time-of-day RVOL baseline.
    rvol_lookback_sessions: int = 10
    #: A fixed symbol list to screen instead of discovering the listed
    #: universe. Set it to run offline, to reproduce a past run, or to watch a
    #: specific basket; leave it empty to screen everything listed.
    universe_file: str = ""
    #: Prefilter. Deliberately looser than either screen — anything dropped
    #: here is never measured properly, so it drops only names no screen could
    #: want. The gap floor is this fraction of screen A's gap gate.
    prefilter_gap_fraction: float = 0.5
    prefilter_min_volume: int = 20_000
    #: A volume surge against the previous session passes the prefilter on its
    #: own, because screen B gates on a surge and asks for no gap at all.
    prefilter_min_volume_ratio: float = 1.5
    #: Ceiling on symbols carried into the expensive stage, ordered by how
    #: hard they are moving. Bounds the run's cost on a day when half the
    #: market gaps.
    max_deep_scan_symbols: int = 120
    #: Operator float overrides, relative to the repository root when relative.
    float_overrides_file: str = "config/float_overrides.csv"
    #: Days a filed float fact is reused from the local cache before EDGAR is
    #: asked again. These facts change a few times a year; this runs daily.
    float_cache_days: int = 7

    def __post_init__(self) -> None:
        if self.max_results < 1:
            raise ValueError("max_results must be at least 1")
        if self.history_retention_days < 1:
            raise ValueError("history_retention_days must be at least 1")
        if self.max_float_age_days < 1:
            raise ValueError("max_float_age_days must be at least 1")
        if self.history_days <= self.catalyst.sma_period:
            raise ValueError("history_days must exceed the catalyst SMA period")
        if self.rvol_lookback_sessions < 1:
            raise ValueError("rvol_lookback_sessions must be at least 1")
        if not (self.low_float.enabled or self.catalyst.enabled):
            raise ValueError("at least one screen must be enabled")
        if not 0 < self.prefilter_gap_fraction <= 1:
            raise ValueError("prefilter_gap_fraction must be within (0, 1]")
        if self.prefilter_min_volume < 0:
            raise ValueError("prefilter_min_volume cannot be negative")
        if self.prefilter_min_volume_ratio <= 0:
            raise ValueError("prefilter_min_volume_ratio must be positive")
        if self.max_deep_scan_symbols < 1:
            raise ValueError("max_deep_scan_symbols must be at least 1")
        if self.float_cache_days < 0:
            raise ValueError("float_cache_days cannot be negative")


# ------------------------------------------------------------------ models


@dataclass(frozen=True)
class BlowingSnapshot:
    """One symbol's morning, as measured. ``None`` always means unmeasured.

    Distinct from ``MarketSnapshot`` because these screens need things the
    scanner never asks for — float, premarket high, session VWAP — and none of
    what it does ask for that they do not use, such as beta against SPY or a
    60-session benchmark series.
    """

    symbol: str
    price: float
    session_date: date
    data_as_of: datetime
    previous_close: float | None = None
    previous_high: float | None = None
    sma: float | None = None
    sma_period: int = 50
    average_volume: float | None = None
    #: Today's cumulative volume through the measurement window — all hours,
    #: not only the regular session.
    session_volume: int | None = None
    #: Today's cumulative volume up to 09:29 ET.
    premarket_volume: int | None = None
    rvol: float | None = None
    rvol_method: str = "unmeasured"
    premarket_high: float | None = None
    #: Which premarket high this is. Inside the premarket the level must
    #: exclude the current bar or the breakout test can never be true; after
    #: the bell it is the settled 04:00–09:29 high. The report says which.
    premarket_high_basis: str = "unmeasured"
    vwap: float | None = None
    vwap_anchor: str = "unmeasured"
    float_shares: float | None = None
    float_basis: str | None = None
    float_as_of: str | None = None
    #: True when ``float_shares`` is shares outstanding rather than float. The
    #: float can only be smaller, so a pass on that basis is still a proven
    #: pass; a fail is not proof of failure, and the report says so.
    float_is_upper_bound: bool = False
    headlines: tuple[str, ...] = ()
    headline_details: tuple[dict[str, Any], ...] = ()
    #: ``False`` when the source publishes no news at all, which is different
    #: from a symbol that genuinely has none today.
    news_available: bool = True
    feed: str = "unknown"
    session_phase: str = "premarket"

    @property
    def gap_percent(self) -> float | None:
        if not self.previous_close:
            return None
        return (self.price / self.previous_close - 1) * 100

    @property
    def volume_multiple(self) -> float | None:
        if not self.average_volume or self.session_volume is None:
            return None
        return self.session_volume / self.average_volume

    @property
    def has_news(self) -> bool | None:
        if not self.news_available:
            return None
        return bool(self.headlines)


@dataclass(frozen=True)
class BlowingCandidate:
    """A symbol that cleared one screen, with every measurement behind it."""

    symbol: str
    screen: str
    price: float
    gap_percent: float | None
    rvol: float | None
    rvol_method: str
    session_volume: int | None
    premarket_volume: int | None
    average_volume: float | None
    volume_multiple: float | None
    float_shares: float | None
    float_basis: str | None
    float_as_of: str | None
    float_is_upper_bound: bool
    premarket_high: float | None
    premarket_high_basis: str
    vwap: float | None
    vwap_anchor: str
    previous_close: float | None
    previous_high: float | None
    sma: float | None
    sma_period: int
    above_premarket_high: bool | None
    above_vwap: bool | None
    above_previous_high: bool | None
    above_sma: bool | None
    keywords: list[str]
    headlines: list[str]
    headline_details: list[dict[str, Any]]
    tier: str
    tier_detail: dict[str, str]
    score: float
    passed_filters: dict[str, bool]
    #: Gates that were required but could not be measured. Empty on every row
    #: of a default run, where an unproven gate rejects the symbol.
    unproven: list[str]
    data_as_of: str
    feed: str
    rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BlowingStocksResult:
    generated_at: str
    session_date: str
    provider: str
    feed: str
    session_phase: str
    universe_size: int
    symbols_examined: int
    low_float: list[BlowingCandidate] = field(default_factory=list)
    catalyst: list[BlowingCandidate] = field(default_factory=list)
    criteria: dict[str, dict[str, str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    rejection_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def symbols_qualified(self) -> int:
        return len({item.symbol for item in (*self.low_float, *self.catalyst)})

    def to_dict(self) -> dict[str, Any]:
        return {
            "report": "blowing-stocks",
            "screener": BlowingStocksScreener.name,
            "generated_at": self.generated_at,
            "session_date": self.session_date,
            "provider": self.provider,
            "feed": self.feed,
            "session_phase": self.session_phase,
            "universe_size": self.universe_size,
            "symbols_examined": self.symbols_examined,
            "symbols_qualified": self.symbols_qualified,
            "criteria": self.criteria,
            "warnings": self.warnings,
            "rejection_counts": self.rejection_counts,
            "low_float": [item.to_dict() for item in self.low_float],
            "catalyst": [item.to_dict() for item in self.catalyst],
            "disclaimer": DISCLAIMER,
        }


DISCLAIMER = (
    "Not investment advice. Every figure here measures what has already printed this "
    "morning; none of it predicts what happens next. Low-float names move violently in both "
    "directions, and the float figures are reconstructed from SEC cover-page filings rather "
    "than a real-time float feed — verify float, price, and news in your broker before "
    "risking anything."
)


# ---------------------------------------------------------------- screening


def _tier_index(value: float | None, *, explosive: float, parabolic: float, higher: bool) -> int:
    """Tier reached on one dimension, as an index into :data:`TIERS`."""
    if value is None:
        return 0
    if higher:
        if value >= parabolic:
            return 2
        return 1 if value >= explosive else 0
    if value <= parabolic:
        return 2
    return 1 if value <= explosive else 0


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class BlowingStocksScreener:
    """Applies both screens to measured snapshots. No I/O, fully deterministic.

    A class rather than a pair of functions because the two screens share
    keyword compilation, float handling, and tiering, and because the caller
    builds it once per run and applies it to every symbol.

    Unmeasured gates never pass. Every required gate except float rejects the
    symbol when it cannot be measured, because a row that cannot say what its
    RVOL or gap was is not evidence of anything. Float is the one gate the
    operator can relax, via ``require_float``, since no free feed publishes it.
    """

    name = "BlowingStocksScreener"

    def __init__(self, config: BlowingStocksConfig | None = None) -> None:
        self.config = config or BlowingStocksConfig()

    # -- shared -----------------------------------------------------------

    def _float_shares(self, snapshot: BlowingSnapshot) -> float | None:
        """Usable float for this symbol, or ``None`` when there is none.

        A filed float older than ``max_float_age_days`` is treated as absent
        rather than current: a share count from two offerings ago is not a
        measurement of this morning's float.
        """
        if snapshot.float_shares is None:
            return None
        if snapshot.float_as_of:
            try:
                measured = date.fromisoformat(snapshot.float_as_of)
            except ValueError:
                return snapshot.float_shares
            if (snapshot.session_date - measured).days > self.config.max_float_age_days:
                return None
        return snapshot.float_shares

    # -- screen A ---------------------------------------------------------

    def screen_low_float(self, snapshot: BlowingSnapshot) -> BlowingCandidate | list[str]:
        """Screen A. Returns the candidate, or the reasons it was rejected."""
        criteria = self.config.low_float
        gates: dict[str, bool] = {}
        unmeasured: list[str] = []

        gates["price"] = criteria.min_price < snapshot.price < criteria.max_price

        float_shares = self._float_shares(snapshot)
        if float_shares is None:
            unmeasured.append("float")
        else:
            gates["float"] = float_shares < criteria.max_float

        if snapshot.rvol is None:
            unmeasured.append("rvol")
        else:
            gates["rvol"] = snapshot.rvol > criteria.min_rvol

        gap = snapshot.gap_percent
        if gap is None:
            unmeasured.append("gap")
        else:
            gates["gap"] = gap > criteria.min_gap_pct

        premarket = snapshot.session_phase == "premarket"
        volume = snapshot.premarket_volume if premarket else snapshot.session_volume
        floor = criteria.min_premarket_volume if premarket else criteria.min_volume
        if volume is None:
            unmeasured.append("volume")
        else:
            gates["volume"] = volume > floor

        keywords = matched_keywords(snapshot.headlines, criteria.keywords)
        has_news = snapshot.has_news
        if criteria.require_news:
            if has_news is None:
                unmeasured.append("news")
            else:
                gates["news"] = has_news
        if criteria.require_keyword:
            gates["keyword"] = bool(keywords)

        above_premarket_high = (
            None if snapshot.premarket_high is None else snapshot.price > snapshot.premarket_high
        )
        above_vwap = None if snapshot.vwap is None else snapshot.price > snapshot.vwap
        if criteria.require_above_premarket_high:
            if above_premarket_high is None:
                unmeasured.append("above_premarket_high")
            else:
                gates["above_premarket_high"] = above_premarket_high
        if criteria.require_above_vwap:
            if above_vwap is None:
                unmeasured.append("above_vwap")
            else:
                gates["above_vwap"] = above_vwap

        # Float is the only gate the operator may leave unproven; anything else
        # unmeasured means the row cannot describe this morning at all.
        blocking = [name for name in unmeasured if name != "float" or self.config.require_float]
        failures = [name for name, passed in gates.items() if not passed]
        if failures or blocking:
            return [*failures, *(f"{name}_unproven" for name in blocking)]

        float_tier = _tier_index(
            float_shares,
            explosive=criteria.best_float,
            parabolic=criteria.parabolic_float,
            higher=False,
        )
        rvol_tier = _tier_index(
            snapshot.rvol,
            explosive=criteria.explosive_rvol,
            parabolic=criteria.parabolic_rvol,
            higher=True,
        )
        # The brief names one step above the gate for the gap, so its top two
        # tiers share a threshold rather than inventing a number it never gave.
        gap_tier = _tier_index(
            gap, explosive=criteria.best_gap_pct, parabolic=criteria.best_gap_pct, higher=True
        )
        # The weakest dimension names the row: a 3M float at RVOL 6 is a good
        # setup, but calling it parabolic on the float alone would describe a
        # stock that is not, in fact, moving parabolically.
        overall = TIERS[min(float_tier, rvol_tier, gap_tier)]

        score = round(
            30 * (0.0 if float_shares is None else _clamp(1 - float_shares / criteria.max_float))
            + 30
            * (0.0 if snapshot.rvol is None else _clamp(snapshot.rvol / criteria.parabolic_rvol))
            + 20 * (0.0 if gap is None else _clamp(gap / max(criteria.best_gap_pct, 0.01)))
            + 10 * (0.0 if volume is None else _clamp(volume / max(floor * 4, 1)))
            + 5 * ((bool(above_premarket_high) + bool(above_vwap)) / 2)
            + 5 * _clamp(len(keywords) / 2),
            2,
        )
        return self._candidate(
            snapshot,
            screen="low_float",
            float_shares=float_shares,
            gates=gates,
            unproven=[name for name in unmeasured if name not in blocking],
            keywords=keywords,
            above_premarket_high=above_premarket_high,
            above_vwap=above_vwap,
            above_previous_high=None,
            above_sma=None,
            tier=overall,
            tier_detail={
                "float": TIERS[float_tier],
                "rvol": TIERS[rvol_tier],
                "gap": TIERS[gap_tier],
            },
            score=score,
        )

    # -- screen B ---------------------------------------------------------

    def screen_catalyst(self, snapshot: BlowingSnapshot) -> BlowingCandidate | list[str]:
        """Screen B. Returns the candidate, or the reasons it was rejected."""
        criteria = self.config.catalyst
        gates: dict[str, bool] = {}
        unmeasured: list[str] = []

        gates["price"] = criteria.min_price < snapshot.price < criteria.max_price

        if snapshot.rvol is None:
            unmeasured.append("rvol")
        else:
            gates["rvol"] = snapshot.rvol > criteria.min_rvol

        multiple = snapshot.volume_multiple
        if multiple is None:
            unmeasured.append("volume_surge")
        else:
            gates["volume_surge"] = multiple > criteria.min_volume_multiple

        keywords = matched_keywords(snapshot.headlines, criteria.keywords)
        if criteria.require_keyword:
            if snapshot.has_news is None:
                unmeasured.append("keyword")
            else:
                gates["keyword"] = bool(keywords)

        above_previous_high = (
            None if snapshot.previous_high is None else snapshot.price > snapshot.previous_high
        )
        above_sma = None if snapshot.sma is None else snapshot.price > snapshot.sma
        if criteria.require_above_previous_high:
            if above_previous_high is None:
                unmeasured.append("above_previous_high")
            else:
                gates["above_previous_high"] = above_previous_high
        if criteria.require_above_sma:
            if above_sma is None:
                unmeasured.append("above_sma")
            else:
                gates["above_sma"] = above_sma

        failures = [name for name, passed in gates.items() if not passed]
        if failures or unmeasured:
            return [*failures, *(f"{name}_unproven" for name in unmeasured)]

        score = round(
            30
            * (
                0.0
                if snapshot.rvol is None
                else _clamp(snapshot.rvol / max(criteria.min_rvol * 4, 0.01))
            )
            + 25 * _clamp((multiple or 0.0) / max(criteria.min_volume_multiple * 3, 0.01))
            + 20 * _clamp(len(keywords) / 2)
            + 12.5 * bool(above_previous_high)
            + 12.5 * bool(above_sma),
            2,
        )
        rvol_tier = _tier_index(
            snapshot.rvol,
            explosive=criteria.min_rvol * 2,
            parabolic=criteria.min_rvol * 4,
            higher=True,
        )
        volume_tier = _tier_index(
            multiple,
            explosive=criteria.min_volume_multiple * 2,
            parabolic=criteria.min_volume_multiple * 4,
            higher=True,
        )
        return self._candidate(
            snapshot,
            screen="catalyst",
            float_shares=self._float_shares(snapshot),
            gates=gates,
            unproven=[],
            keywords=keywords,
            above_premarket_high=None,
            above_vwap=None,
            above_previous_high=above_previous_high,
            above_sma=above_sma,
            tier=TIERS[min(rvol_tier, volume_tier)],
            tier_detail={"rvol": TIERS[rvol_tier], "volume": TIERS[volume_tier]},
            score=score,
        )

    # -- construction -----------------------------------------------------

    def _candidate(
        self,
        snapshot: BlowingSnapshot,
        *,
        screen: str,
        float_shares: float | None,
        gates: dict[str, bool],
        unproven: Sequence[str],
        keywords: Sequence[str],
        above_premarket_high: bool | None,
        above_vwap: bool | None,
        above_previous_high: bool | None,
        above_sma: bool | None,
        tier: str,
        tier_detail: dict[str, str],
        score: float,
    ) -> BlowingCandidate:
        return BlowingCandidate(
            symbol=snapshot.symbol,
            screen=screen,
            price=round(snapshot.price, 4),
            gap_percent=None if snapshot.gap_percent is None else round(snapshot.gap_percent, 2),
            rvol=None if snapshot.rvol is None else round(snapshot.rvol, 2),
            rvol_method=snapshot.rvol_method,
            session_volume=snapshot.session_volume,
            premarket_volume=snapshot.premarket_volume,
            average_volume=(
                None if snapshot.average_volume is None else round(snapshot.average_volume, 0)
            ),
            volume_multiple=(
                None if snapshot.volume_multiple is None else round(snapshot.volume_multiple, 2)
            ),
            float_shares=float_shares,
            float_basis=None if float_shares is None else snapshot.float_basis,
            float_as_of=None if float_shares is None else snapshot.float_as_of,
            float_is_upper_bound=bool(float_shares is not None and snapshot.float_is_upper_bound),
            premarket_high=snapshot.premarket_high,
            premarket_high_basis=snapshot.premarket_high_basis,
            vwap=None if snapshot.vwap is None else round(snapshot.vwap, 4),
            vwap_anchor=snapshot.vwap_anchor,
            previous_close=snapshot.previous_close,
            previous_high=snapshot.previous_high,
            sma=None if snapshot.sma is None else round(snapshot.sma, 4),
            sma_period=snapshot.sma_period,
            above_premarket_high=above_premarket_high,
            above_vwap=above_vwap,
            above_previous_high=above_previous_high,
            above_sma=above_sma,
            keywords=list(keywords),
            headlines=list(snapshot.headlines),
            headline_details=[dict(item) for item in snapshot.headline_details],
            tier=tier,
            tier_detail=tier_detail,
            score=score,
            passed_filters=dict(gates),
            unproven=list(unproven),
            data_as_of=snapshot.data_as_of.isoformat(),
            feed=snapshot.feed,
        )

    # -- run --------------------------------------------------------------

    def run(
        self,
        snapshots: Sequence[BlowingSnapshot],
        *,
        provider: str,
        generated_at: str,
        session_date: date,
        feed: str = "unknown",
        session_phase: str = "premarket",
        universe_size: int = 0,
        warnings: Sequence[str] = (),
    ) -> BlowingStocksResult:
        low_float: list[BlowingCandidate] = []
        catalyst: list[BlowingCandidate] = []
        rejections: dict[str, dict[str, int]] = {"low_float": {}, "catalyst": {}}

        for snapshot in snapshots:
            for enabled, screen, bucket in (
                (self.config.low_float.enabled, "low_float", low_float),
                (self.config.catalyst.enabled, "catalyst", catalyst),
            ):
                if not enabled:
                    continue
                evaluate = (
                    self.screen_low_float if screen == "low_float" else self.screen_catalyst
                )
                outcome = evaluate(snapshot)
                if isinstance(outcome, BlowingCandidate):
                    bucket.append(outcome)
                else:
                    for reason in outcome:
                        rejections[screen][reason] = rejections[screen].get(reason, 0) + 1

        ranked_low = _rank(low_float, self.config.max_results)
        ranked_catalyst = _rank(catalyst, self.config.max_results)
        notes = list(warnings)
        if not self.config.require_float:
            notes.append(
                "require_float is off: any row whose 'unproven' column lists float was NOT "
                "screened on float. 'Low float' is an unverified claim for those names."
            )
        if any(item.float_is_upper_bound for item in (*ranked_low, *ranked_catalyst)):
            notes.append(
                "Some float figures are shares outstanding, an upper bound on float. A name "
                "that passed on that basis really is under the threshold; names rejected on it "
                "may still have a small float the filings do not break out."
            )
        return BlowingStocksResult(
            generated_at=generated_at,
            session_date=session_date.isoformat(),
            provider=provider,
            feed=feed,
            session_phase=session_phase,
            universe_size=universe_size,
            symbols_examined=len(snapshots),
            low_float=ranked_low,
            catalyst=ranked_catalyst,
            criteria=criteria_descriptions(self.config),
            warnings=notes,
            rejection_counts={
                screen: dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
                for screen, counts in rejections.items()
            },
        )


def _rank(found: list[BlowingCandidate], cap: int) -> list[BlowingCandidate]:
    """Rank by score, strongest first, then cap. Never padded."""
    ordered = sorted(found, key=lambda item: (-item.score, item.symbol))[:cap]
    return [
        BlowingCandidate(**{**item.__dict__, "rank": position})
        for position, item in enumerate(ordered, start=1)
    ]


def criteria_descriptions(config: BlowingStocksConfig) -> dict[str, dict[str, str]]:
    """Human-readable rendering of every gate, for the report header."""
    low, cat = config.low_float, config.catalyst
    described: dict[str, dict[str, str]] = {}
    if low.enabled:
        described["low_float"] = {
            "price": f"${low.min_price:g} < last < ${low.max_price:g}",
            "float": (
                f"<{low.max_float:,.0f} shares "
                f"(best <{low.best_float:,.0f}, parabolic <{low.parabolic_float:,.0f})"
            ),
            "rvol": (
                f">{low.min_rvol:g} (explosive >{low.explosive_rvol:g}, "
                f"parabolic >{low.parabolic_rvol:g})"
            ),
            "gap": f">{low.min_gap_pct:g}% (best >{low.best_gap_pct:g}%)",
            "volume": (
                f">{low.min_volume:,} shares today, "
                f">{low.min_premarket_volume:,} on a premarket run"
            ),
            "news": (
                ("has news required; " if low.require_news else "news optional; ")
                + ("keyword required; " if low.require_keyword else "keywords rank only; ")
                + ", ".join(low.keywords)
            ),
            "breakout": " and ".join(
                filter(
                    None,
                    [
                        "price > premarket high" if low.require_above_premarket_high else "",
                        "price > VWAP" if low.require_above_vwap else "",
                    ],
                )
            )
            or "not required",
        }
    if cat.enabled:
        described["catalyst"] = {
            "price": f"${cat.min_price:g} < last < ${cat.max_price:g}",
            "rvol": f">{cat.min_rvol:g}",
            "volume_surge": (
                f">{cat.min_volume_multiple:g}x the {cat.volume_average_days}-session mean volume"
            ),
            "news": (
                ("keyword required; " if cat.require_keyword else "keywords rank only; ")
                + ", ".join(cat.keywords)
            ),
            "breakout": " and ".join(
                filter(
                    None,
                    [
                        "price > yesterday's high" if cat.require_above_previous_high else "",
                        f"price > {cat.sma_period}-session SMA" if cat.require_above_sma else "",
                    ],
                )
            )
            or "not required",
        }
    described["run"] = {
        "float_source": (
            "SEC cover-page filings plus operator overrides; an unprovable float "
            + ("rejects the symbol" if config.require_float else "is flagged, not rejected")
        ),
        "results": f"up to {config.max_results} per screen; never padded",
        "history": f"dated reports kept {config.history_retention_days} days",
        "nature": "measurements of this morning; not advice and not a forecast",
    }
    return described


def config_from_mapping(payload: Mapping[str, Any]) -> BlowingStocksConfig:
    """Build the config from a parsed ``[blowing_stocks]`` TOML table.

    Unknown keys raise rather than being ignored, so a typo in the config is a
    startup error instead of a gate that silently never applied.
    """
    values = dict(payload)
    sections = {
        "low_float": (dict(values.pop("low_float", {}) or {}), LowFloatCriteria),
        "catalyst": (dict(values.pop("catalyst", {}) or {}), CatalystCriteria),
    }
    for name, (section, klass) in sections.items():
        unknown = set(section) - set(klass.__dataclass_fields__)
        if unknown:
            raise ValueError(
                f"Unknown blowing_stocks.{name} config keys: {', '.join(sorted(unknown))}"
            )
        if "keywords" in section:
            section["keywords"] = tuple(section["keywords"])
    unknown = set(values) - set(BlowingStocksConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown blowing_stocks config keys: {', '.join(sorted(unknown))}")
    return BlowingStocksConfig(
        low_float=LowFloatCriteria(**sections["low_float"][0]),
        catalyst=CatalystCriteria(**sections["catalyst"][0]),
        **values,
    )
