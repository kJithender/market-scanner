"""Provider-independent scan orchestration."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from statistics import fmean

from market_scanner.indicators import (
    atr_percent,
    key_levels,
    relative_volume,
    return_beta,
    spread_percent,
    trend_structure,
)
from market_scanner.models import Candidate, MarketSnapshot, ScanConfig, ScanResult
from market_scanner.scoring import all_eligible, eligibility, rank_score


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
    }


def _evaluate(snapshot: MarketSnapshot, config: ScanConfig) -> Candidate | tuple[str, ...]:
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
    score = rank_score(
        rvol=rvol,
        atr_pct=atr_pct,
        beta=beta,
        spread_pct=spread_pct,
        trend_score=trend_score,
        catalyst_count=len(catalysts),
        gap_pct=gap_pct,
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
    )


async def scan_market(provider, symbols: list[str], config: ScanConfig, as_of=None) -> ScanResult:
    now = as_of or datetime.now(UTC)
    snapshots, provider_warnings = await provider.get_snapshots(symbols, now, config)
    qualified: list[Candidate] = []
    rejected: Counter[str] = Counter()
    for snapshot in snapshots:
        evaluation = _evaluate(snapshot, config)
        if isinstance(evaluation, Candidate):
            qualified.append(evaluation)
        else:
            rejected.update(evaluation)
    qualified.sort(key=lambda item: (-item.score, item.symbol))
    qualified = qualified[: config.watchlist_size]
    qualified = [
        Candidate(**{**item.__dict__, "rank": rank}) for rank, item in enumerate(qualified, 1)
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
    )
