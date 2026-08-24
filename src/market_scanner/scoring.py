"""Hard eligibility gates and transparent ranking."""

from __future__ import annotations

from collections.abc import Mapping

from market_scanner.fundamentals import Fundamentals
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
    momentum_pct: float | None = None,
    volume_confirmation: float | None = None,
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
    if config.min_momentum_pct and momentum_pct is not None:
        gates["momentum"] = momentum_pct >= config.min_momentum_pct
    if config.min_volume_confirmation and volume_confirmation is not None:
        gates["volume_confirmation"] = volume_confirmation >= config.min_volume_confirmation
    gates["clean_trend"] = clean_trend
    gates["clear_levels"] = clear_levels
    gates["catalyst"] = has_catalyst or not config.require_catalyst
    return gates


def all_eligible(gates: Mapping[str, bool]) -> bool:
    return bool(gates) and all(gates.values())


def fundamental_gates(
    *, config: ScanConfig, fundamentals: Fundamentals | None, stale: bool = False
) -> dict[str, bool]:
    """Evaluate fundamental requirements the filings can actually support.

    Mirrors the technical gates: a metric the filings never reported leaves its
    gate out of the mapping entirely, so it can neither pass nor fail. The only
    gate that fires on absence is ``fundamentals_available``, and only when the
    operator explicitly demanded fundamentals via ``require_fundamentals``.
    """
    if not config.use_fundamentals:
        return {}
    gates: dict[str, bool] = {}
    if config.require_fundamentals:
        gates["fundamentals_available"] = fundamentals is not None and fundamentals.coverage > 0
        gates["fundamentals_fresh"] = not stale
    if fundamentals is None:
        return gates
    if fundamentals.revenue_growth_pct is not None:
        gates["revenue_growth"] = fundamentals.revenue_growth_pct >= config.min_revenue_growth_pct
    if fundamentals.debt_to_equity is not None:
        gates["debt_to_equity"] = fundamentals.debt_to_equity <= config.max_debt_to_equity
    if config.require_profitable and fundamentals.net_margin_pct is not None:
        gates["profitable"] = fundamentals.net_margin_pct > 0
    return gates


def fundamental_score(fundamentals: Fundamentals | None) -> float | None:
    """Score 0–100 from filed fundamentals, or ``None`` if nothing is filed.

    Each component is skipped when its metric is unavailable and the result is
    rescaled across the components that were measurable, so a company with
    partial coverage is not penalised for what its filings omit. The caller
    still sees ``coverage`` and can weigh a thin score accordingly.
    """
    if fundamentals is None:
        return None
    components: list[tuple[float, float]] = []  # (weight, 0..1 value)
    growth = fundamentals.revenue_growth_pct
    if growth is not None:
        components.append((35.0, min(max(growth, 0.0) / 40.0, 1.0)))
    eps_growth = fundamentals.eps_growth_pct
    if eps_growth is not None:
        components.append((20.0, min(max(eps_growth, 0.0) / 50.0, 1.0)))
    margin = fundamentals.net_margin_pct
    if margin is not None:
        components.append((25.0, min(max(margin, 0.0) / 25.0, 1.0)))
    roe = fundamentals.return_on_equity_pct
    if roe is not None:
        components.append((10.0, min(max(roe, 0.0) / 30.0, 1.0)))
    leverage = fundamentals.debt_to_equity
    if leverage is not None:
        components.append((10.0, max(0.0, 1.0 - min(leverage, 2.0) / 2.0)))
    if not components:
        return None
    total_weight = sum(weight for weight, _ in components)
    earned = sum(weight * value for weight, value in components)
    return round(earned / total_weight * 100, 2)


def rank_score(
    *,
    rvol: float | None,
    atr_pct: float,
    beta: float,
    spread_pct: float | None,
    trend_score: float,
    catalyst_count: int,
    gap_pct: float,
    fundamental: float | None = None,
    fundamental_weight: float = 0.35,
) -> float:
    """Rank eligible names; no score can bypass a failed hard gate.

    Unmeasured metrics score zero for their component so a name is never
    ranked higher for evidence the provider never supplied.

    When a fundamental score exists it is blended in at ``fundamental_weight``
    so the ranking reflects momentum *and* business quality. When it does not,
    the technical score stands alone rather than being diluted toward zero —
    an unfiled metric must not act as a fundamental failing.
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
    if fundamental is not None:
        weight = min(max(fundamental_weight, 0.0), 1.0)
        score = score * (1 - weight) + fundamental * weight
    return round(score, 2)


def volatility_score(
    *,
    atr_pct: float,
    rvol: float | None,
    spread_pct: float | None,
    gap_pct: float,
    volume_confirmation: float | None,
) -> float:
    """Rank the daily volatility list, 0–100.

    Separate from :func:`rank_score` on purpose, and not comparable to it: this
    answers "how tradable is the movement today" rather than "how good is the
    setup". Range is the raw material, so ATR carries the most weight, but a
    wide spread is subtracted rather than merely under-rewarded — on a short
    hold the spread is paid in full on entry and exit, so it can erase the
    move the volatility was supposed to provide.

    An unmeasured metric earns nothing for its component, exactly as in
    :func:`rank_score`, so a name is never ranked higher for missing evidence.
    """
    score = (
        min(atr_pct / 10, 1) * 45
        + (0.0 if rvol is None else min(rvol / 3, 1) * 25)
        + min(abs(gap_pct) / 5, 1) * 15
        + (
            0.0
            if volume_confirmation is None
            else min(max(volume_confirmation - 1, 0) / 1.0, 1) * 15
        )
    )
    if spread_pct is not None:
        # Charged against the range it eats: a 0.4% spread costs far more on a
        # 2% ATR name than on an 8% one, and a flat penalty would miss that.
        score -= min(spread_pct * 2 / max(atr_pct, 0.01), 1) * 25
    return round(max(score, 0.0), 2)
