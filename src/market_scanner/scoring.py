"""Hard eligibility gates and transparent ranking."""

from __future__ import annotations

from collections.abc import Mapping

from market_scanner.models import ScanConfig


def eligibility(
    *,
    config: ScanConfig,
    price: float,
    average_volume: float,
    spread_pct: float | None,
    atr_pct: float,
    beta: float,
    rvol: float | None,
    clean_trend: bool,
    clear_levels: bool,
    has_catalyst: bool,
) -> dict[str, bool]:
    """Evaluate every named requirement; strict inequalities match the brief.

    A ``None`` metric means the provider published no evidence for that gate.
    The gate is then omitted entirely rather than recorded as a pass, so an
    unmeasurable requirement can never be mistaken for a satisfied one.
    """
    gates = {
        "price": config.min_price <= price <= config.max_price,
        "average_volume": average_volume > config.min_average_volume,
    }
    if spread_pct is not None:
        gates["tight_spread"] = spread_pct <= config.max_spread_pct
    gates["atr"] = config.min_atr_pct <= atr_pct <= config.max_atr_pct
    gates["beta"] = beta > config.min_beta
    if rvol is not None:
        gates["rvol"] = rvol > config.min_rvol
    gates["clean_trend"] = clean_trend
    gates["clear_levels"] = clear_levels
    gates["catalyst"] = has_catalyst or not config.require_catalyst
    return gates


def all_eligible(gates: Mapping[str, bool]) -> bool:
    return bool(gates) and all(gates.values())


def rank_score(
    *,
    rvol: float | None,
    atr_pct: float,
    beta: float,
    spread_pct: float | None,
    trend_score: float,
    catalyst_count: int,
    gap_pct: float,
) -> float:
    """Rank eligible names; no score can bypass a failed hard gate.

    Unmeasured metrics score zero for their component so a name is never
    ranked higher for evidence the provider never supplied.
    """
    volatility_fit = max(0.0, 1 - abs(atr_pct - 3.5) / 1.5)
    score = (
        (0.0 if rvol is None else min(rvol / 3, 1) * 30)
        + volatility_fit * 15
        + min(max(beta - 1, 0) / 1.5, 1) * 10
        + (0.0 if spread_pct is None else max(0.0, 1 - spread_pct / 0.30) * 10)
        + min(trend_score / 100, 1) * 20
        + min(catalyst_count, 2) / 2 * 10
        + min(abs(gap_pct) / 5, 1) * 5
    )
    return round(score, 2)
