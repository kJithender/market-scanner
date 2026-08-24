"""Provider-independent scan orchestration."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from statistics import fmean

from market_scanner.fundamentals import Fundamentals, stale_after
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
from market_scanner.models import (
    Candidate,
    MarketSnapshot,
    ScanConfig,
    ScanResult,
    VolatilityCandidate,
)
from market_scanner.scoring import (
    all_eligible,
    eligibility,
    fundamental_gates,
    fundamental_score,
    rank_score,
    volatility_score,
)


def filter_descriptions(config: ScanConfig) -> dict[str, str]:
    return {
        "price": f"${config.min_price:g}–${config.max_price:g}",
        "average_volume": f">{config.min_average_volume:,} shares (20-session mean)",
        "spread": f"≤{config.max_spread_pct:g}% of quote midpoint",
        "atr": f"{config.min_atr_pct:g}–{config.max_atr_pct:g}% (14 sessions)",
        "beta": f">{config.min_beta:g} vs SPY (60 sessions)",
        "rvol": f">{config.min_rvol:g}, same premarket time-window",
        "technical_structure": "clean MA trend + clear 20-session levels",
        "catalyst": f"news, upcoming earnings, or ≥{config.min_gap_pct:g}% gap"
        + (" (required)" if config.require_catalyst else " (ranking context)"),
        "watchlist_size": f"up to {config.watchlist_size}; never padded",
        **(
            {
                "volume_confirmation": (
                    f"≥{config.min_volume_confirmation:g}× prior "
                    f"{config.volume_confirmation_lookback_days}-session mean volume"
                )
            }
            if config.min_volume_confirmation
            else {}
        ),
        **(
            {
                "volatility_list": (
                    f"separate daily list: ATR ≥{config.volatility_min_atr_pct:g}%, "
                    f">{config.volatility_min_average_volume:,} avg shares, "
                    f"${config.min_price:g}–${config.max_price:g}, "
                    f"spread ≤{config.volatility_max_spread_pct:g}%, "
                    f"RVOL ≥{config.volatility_min_rvol:g}; "
                    "hard gates NOT applied"
                )
            }
            if config.volatility_list_enabled
            else {}
        ),
        **(
            {
                "momentum": (
                    f"≥{config.min_momentum_pct:g}% over "
                    f"{config.momentum_lookback_days} sessions"
                )
            }
            if config.min_momentum_pct
            else {}
        ),
        **(
            {
                "revenue_growth": (
                    f"≥{config.min_revenue_growth_pct:g}% latest quarter YoY (SEC XBRL)"
                ),
                "debt_to_equity": f"≤{config.max_debt_to_equity:g} (SEC XBRL)",
                "profitability": (
                    "positive TTM net margin required"
                    if config.require_profitable
                    else "ranking context only"
                ),
                "fundamentals_source": (
                    "SEC EDGAR filed company facts; an unfiled metric is left unevaluated"
                ),
            }
            if config.use_fundamentals
            else {}
        ),
    }


def _evaluate(
    snapshot: MarketSnapshot,
    config: ScanConfig,
    fundamentals: Fundamentals | None = None,
    now: datetime | None = None,
) -> Candidate | tuple[str, ...]:
    if len(snapshot.daily_bars) < max(config.history_days - 5, 61):
        return ("insufficient_history",)
    unverified = set(snapshot.unverified_gates)
    try:
        price = snapshot.price
        average_volume = fmean(bar.volume for bar in snapshot.daily_bars[-20:])
        spread_pct = (
            None
            if "tight_spread" in unverified
            else spread_percent(snapshot.quote.bid, snapshot.quote.ask)
        )
        atr_pct = atr_percent(snapshot.daily_bars, price)
        beta = return_beta(snapshot.daily_bars, snapshot.benchmark_bars)
        rvol = (
            None
            if "rvol" in unverified
            else relative_volume(
                snapshot.current_premarket_volume,
                snapshot.historical_premarket_volumes,
            )
        )
        momentum_pct = (
            momentum_percent(snapshot.daily_bars, config.momentum_lookback_days)
            if len(snapshot.daily_bars) > config.momentum_lookback_days
            else None
        )
        try:
            spike = volume_spike(snapshot.daily_bars, config.volume_spike_lookback_days)
        except (ValueError, ZeroDivisionError):
            spike = None
        try:
            rsi_value = relative_strength_index(snapshot.daily_bars, config.rsi_period)
        except (ValueError, ZeroDivisionError):
            rsi_value = None
        try:
            confirmation = volume_trend_confirmation(
                snapshot.daily_bars, config.volume_confirmation_lookback_days
            )
        except (ValueError, ZeroDivisionError):
            confirmation = None
        clean_trend, direction, trend_score = trend_structure(snapshot.daily_bars)
        clear_levels, support, resistance = key_levels(
            snapshot.daily_bars, config.level_lookback_days
        )
    except (ValueError, ZeroDivisionError):
        return ("invalid_or_insufficient_data",)

    previous_close = snapshot.daily_bars[-1].close
    gap_pct = (price / previous_close - 1) * 100 if previous_close else 0.0
    catalysts = list(snapshot.catalysts)
    has_gap = abs(gap_pct) >= config.min_gap_pct
    if has_gap:
        from market_scanner.models import Catalyst

        catalysts.append(Catalyst("gap", f"Premarket gap {gap_pct:+.2f}%"))
    gates = eligibility(
        config=config,
        price=price,
        average_volume=average_volume,
        spread_pct=spread_pct,
        atr_pct=atr_pct,
        beta=beta,
        rvol=rvol,
        clean_trend=clean_trend,
        clear_levels=clear_levels,
        has_catalyst=bool(catalysts),
        momentum_pct=momentum_pct,
        volume_confirmation=confirmation,
    )
    is_stale = (
        stale_after(fundamentals, now or datetime.now(UTC), config.max_fundamentals_age_days)
        if fundamentals is not None
        else False
    )
    gates.update(
        fundamental_gates(config=config, fundamentals=fundamentals, stale=is_stale)
    )
    if not all_eligible(gates):
        return tuple(name for name, passed in gates.items() if not passed)

    atr_value = atr_pct / 100 * price
    rvol_text = "unverified" if rvol is None else f"{rvol:.2f}"
    if direction == "downtrend":
        stop = max(price + atr_value, resistance)
        per_share_risk = stop - price
        target = price - per_share_risk * config.reward_to_risk
        thesis = (
            f"Clean downtrend; watch rejection below ${resistance:.2f} with "
            f"RVOL {rvol_text} and {gap_pct:+.2f}% gap."
        )
    else:
        stop = max(price - atr_value, max(support, 0.01))
        per_share_risk = price - stop
        target = price + per_share_risk * config.reward_to_risk
        thesis = (
            f"Clean uptrend; watch hold above ${support:.2f} with "
            f"RVOL {rvol_text} and {gap_pct:+.2f}% gap."
        )
    shares = int(config.risk_per_trade_dollars // per_share_risk) if per_share_risk > 0 else 0
    eps_ttm = fundamentals.eps_ttm if fundamentals else None
    # A negative or zero denominator makes P/E meaningless, not attractive.
    pe_ratio = round(price / eps_ttm, 2) if eps_ttm and eps_ttm > 0 else None
    fundamental = fundamental_score(fundamentals) if config.use_fundamentals else None
    score = rank_score(
        rvol=rvol,
        atr_pct=atr_pct,
        beta=beta,
        spread_pct=spread_pct,
        trend_score=trend_score,
        catalyst_count=len(catalysts),
        gap_pct=gap_pct,
        fundamental=fundamental,
    )
    as_of = snapshot.data_as_of or snapshot.quote.timestamp
    return Candidate(
        symbol=snapshot.symbol,
        price=round(price, 4),
        avg_volume=round(average_volume, 0),
        current_volume=snapshot.current_premarket_volume,
        rvol=None if rvol is None else round(rvol, 3),
        rvol_method=snapshot.rvol_method,
        atr_percent=round(atr_pct, 3),
        beta=round(beta, 3),
        spread_percent=None if spread_pct is None else round(spread_pct, 4),
        gap_percent=round(gap_pct, 3),
        momentum_percent=None if momentum_pct is None else round(momentum_pct, 2),
        volume_spike=None if spike is None else round(spike, 2),
        volume_confirmation=None if confirmation is None else round(confirmation, 2),
        rsi=None if rsi_value is None else round(rsi_value, 1),
        pe_ratio=pe_ratio,
        trend=direction,
        trend_score=round(trend_score, 2),
        levels={"support": round(support, 2), "resistance": round(resistance, 2)},
        catalysts=[catalyst.description for catalyst in catalysts],
        catalyst_details=[
            {
                "kind": catalyst.kind,
                "description": catalyst.description,
                "timestamp": catalyst.timestamp.isoformat() if catalyst.timestamp else None,
                "url": catalyst.url,
            }
            for catalyst in catalysts
        ],
        thesis=thesis,
        stop=round(stop, 2),
        target=round(target, 2),
        risk=(
            f"${per_share_risk:.2f}/share; {shares} shares at "
            f"${config.risk_per_trade_dollars:.0f} max planned risk"
        ),
        score=score,
        data_as_of=as_of.isoformat(),
        passed_filters=gates,
        fundamentals=(
            fundamentals.to_dict() if config.use_fundamentals and fundamentals else None
        ),
        fundamental_score=fundamental,
    )


def _volatility_entry(
    snapshot: MarketSnapshot, config: ScanConfig
) -> VolatilityCandidate | None:
    """Screen one symbol for the daily high-volatility list.

    Runs independently of :func:`_evaluate` rather than reusing its result, for
    two reasons. The hard gates reject most volatile names for reasons that are
    irrelevant to a scalp — no filed revenue growth, no news catalyst, a trend
    that is not "clean" — so a name that failed them has to remain reachable
    here. And keeping the paths separate means a rejection in the watchlist
    logic can never empty this list, which is the list the operator still needs
    on the days nothing qualifies.

    Applies a tradability floor and a volatility bar, nothing else. Returns
    ``None`` when the symbol misses the floor or the feed cannot support the
    measurement.
    """
    unverified = set(snapshot.unverified_gates)
    try:
        price = snapshot.price
        if not config.min_price <= price <= config.max_price:
            return None
        average_volume = fmean(bar.volume for bar in snapshot.daily_bars[-20:])
        if average_volume <= config.volatility_min_average_volume:
            return None
        atr_pct = atr_percent(snapshot.daily_bars, price)
    except (ValueError, ZeroDivisionError, IndexError):
        return None
    if atr_pct < config.volatility_min_atr_pct:
        return None

    # Both checks are skipped when the provider publishes nothing for them, so
    # an unmeasurable spread or RVOL leaves the name on the list unproven
    # rather than silently passing or silently vanishing.
    spread_pct = (
        None
        if "tight_spread" in unverified
        else spread_percent(snapshot.quote.bid, snapshot.quote.ask)
    )
    if spread_pct is not None and spread_pct > config.volatility_max_spread_pct:
        return None
    rvol = (
        None
        if "rvol" in unverified
        else relative_volume(
            snapshot.current_premarket_volume, snapshot.historical_premarket_volumes
        )
    )
    if rvol is not None and rvol < config.volatility_min_rvol:
        return None

    try:
        confirmation = volume_trend_confirmation(
            snapshot.daily_bars, config.volume_confirmation_lookback_days
        )
    except (ValueError, ZeroDivisionError):
        confirmation = None
    try:
        _, direction, _ = trend_structure(snapshot.daily_bars)
    except (ValueError, ZeroDivisionError):
        direction = "unknown"
    previous_close = snapshot.daily_bars[-1].close
    gap_pct = (price / previous_close - 1) * 100 if previous_close else 0.0
    as_of = snapshot.data_as_of or snapshot.quote.timestamp
    return VolatilityCandidate(
        symbol=snapshot.symbol,
        price=round(price, 4),
        avg_volume=round(average_volume, 0),
        atr_percent=round(atr_pct, 3),
        rvol=None if rvol is None else round(rvol, 3),
        spread_percent=None if spread_pct is None else round(spread_pct, 4),
        gap_percent=round(gap_pct, 3),
        volume_confirmation=None if confirmation is None else round(confirmation, 2),
        trend=direction,
        volatility_score=volatility_score(
            atr_pct=atr_pct,
            rvol=rvol,
            spread_pct=spread_pct,
            gap_pct=gap_pct,
            volume_confirmation=confirmation,
        ),
        data_as_of=as_of.isoformat(),
    )


async def scan_market(
    provider,
    symbols: list[str],
    config: ScanConfig,
    as_of=None,
    fundamentals_provider=None,
) -> ScanResult:
    now = as_of or datetime.now(UTC)
    snapshots, provider_warnings = await provider.get_snapshots(symbols, now, config)

    facts: dict[str, Fundamentals] = {}
    if config.use_fundamentals and fundamentals_provider is not None and snapshots:
        # Only fetch for symbols that survived the price feed; fundamentals are
        # one HTTP round trip per company and the rest cannot qualify anyway.
        facts, fundamental_warnings = await asyncio.to_thread(
            fundamentals_provider.fetch, [snapshot.symbol for snapshot in snapshots]
        )
        provider_warnings = [*provider_warnings, *fundamental_warnings]

    qualified: list[Candidate] = []
    rejected: Counter[str] = Counter()
    volatile: list[VolatilityCandidate] = []
    for snapshot in snapshots:
        evaluation = _evaluate(snapshot, config, facts.get(snapshot.symbol), now)
        if isinstance(evaluation, Candidate):
            qualified.append(evaluation)
        else:
            rejected.update(evaluation)
        if config.volatility_list_enabled:
            entry = _volatility_entry(snapshot, config)
            if entry is not None:
                volatile.append(entry)
    qualified.sort(key=lambda item: (-item.score, item.symbol))
    qualified = qualified[: config.watchlist_size]
    qualified = [
        Candidate(**{**item.__dict__, "rank": rank}) for rank, item in enumerate(qualified, 1)
    ]
    watchlisted = {item.symbol for item in qualified}
    volatile.sort(key=lambda item: (-item.volatility_score, item.symbol))
    volatile = volatile[: config.volatility_list_size]
    volatile = [
        VolatilityCandidate(
            **{
                **item.__dict__,
                "rank": rank,
                "on_watchlist": item.symbol in watchlisted,
            }
        )
        for rank, item in enumerate(volatile, 1)
    ]
    warnings = list(provider_warnings)
    filters = filter_descriptions(config)
    unverified = sorted({gate for snapshot in snapshots for gate in snapshot.unverified_gates})
    if unverified:
        filter_keys = {"tight_spread": "spread"}
        for gate in unverified:
            key = filter_keys.get(gate, gate)
            if key in filters:
                filters[key] += " — NOT VERIFIED: provider published no data"
        warnings.append(
            "Hard gates left unevaluated because the provider publishes no data for them: "
            + ", ".join(unverified)
            + ". Those requirements are unproven for every symbol listed below."
        )
    if len(qualified) < config.minimum_watchlist_size:
        warnings.append(
            f"Only {len(qualified)} symbols passed every hard gate; watchlist was not padded."
        )
    if config.volatility_list_enabled:
        warnings.append(
            f"The high-volatility list ({len(volatile)} names) is screened on liquidity and "
            f"ATR ≥{config.volatility_min_atr_pct:g}% only. Those names have NOT passed the "
            "hard gates, carry no thesis, stop, or target, and are not a watchlist."
        )
    data_times = [item.data_as_of for item in qualified]
    return ScanResult(
        candidates=qualified,
        generated_at=datetime.now(UTC).isoformat(),
        data_as_of=min(data_times) if data_times else None,
        provider=provider.name,
        symbols_scanned=len(symbols),
        symbols_qualified=len(qualified),
        filters=filters,
        warnings=warnings,
        rejection_counts=dict(sorted(rejected.items())),
        volatility_candidates=volatile,
    )
