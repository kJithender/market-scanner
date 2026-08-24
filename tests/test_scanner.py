from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

from market_scanner.config import load_symbols
from market_scanner.fundamentals import Fundamentals
from market_scanner.models import Candidate, ScanConfig
from market_scanner.providers import DemoProvider
from market_scanner.scanner import _evaluate, _volatility_entry, scan_market
from market_scanner.scoring import all_eligible, eligibility, rank_score


def test_exact_filter_boundaries_and_strict_gates() -> None:
    config = ScanConfig()
    base = dict(
        config=config,
        price=5.0,
        average_volume=1_000_001,
        spread_pct=0.30,
        atr_pct=2.0,
        beta=1.01,
        rvol=1.51,
        clean_trend=True,
        clear_levels=True,
        has_catalyst=True,
    )
    assert all_eligible(eligibility(**base))
    for field, boundary in (
        ("average_volume", 1_000_000),
        ("beta", 1.0),
        ("rvol", 1.5),
    ):
        values = {**base, field: boundary}
        assert not eligibility(**values)[field if field != "average_volume" else "average_volume"]
    assert eligibility(**{**base, "price": 150.0})["price"]
    assert eligibility(**{**base, "atr_pct": 5.0})["atr"]
    assert not eligibility(**{**base, "spread_pct": 0.301})["tight_spread"]
    assert not eligibility(**{**base, "has_catalyst": False})["catalyst"]


def test_demo_scan_caps_at_configured_size_and_every_result_passes_every_gate() -> None:
    config = ScanConfig()
    symbols = load_symbols()
    result = asyncio.run(scan_market(DemoProvider(), symbols, config))
    assert result.symbols_scanned == len(symbols)
    # The cap is configurable, so assert against the config rather than a
    # literal; the universe is deliberately far larger than the watchlist.
    assert len(result.candidates) == config.watchlist_size
    assert [item.rank for item in result.candidates] == list(
        range(1, config.watchlist_size + 1)
    )
    assert all(all(item.passed_filters.values()) for item in result.candidates)
    assert all(item.avg_volume > 1_000_000 for item in result.candidates)
    assert all(item.rvol > 1.5 and item.beta > 1 for item in result.candidates)
    assert all(2 <= item.atr_percent <= 5 for item in result.candidates)
    assert all(
        item.thesis and item.stop and item.target and item.risk for item in result.candidates
    )


def test_scanner_never_pads_when_fewer_than_ten_pass() -> None:
    result = asyncio.run(scan_market(DemoProvider(), ["AMD"], ScanConfig()))
    assert len(result.candidates) == 1
    assert any("not padded" in warning for warning in result.warnings)


def test_unmeasurable_gates_are_omitted_not_passed() -> None:
    base = dict(
        config=ScanConfig(),
        price=50.0,
        average_volume=2_000_000,
        atr_pct=3.0,
        beta=1.2,
        clean_trend=True,
        clear_levels=True,
        has_catalyst=True,
    )
    gates = eligibility(**base, spread_pct=None, rvol=None)
    assert "tight_spread" not in gates, "an unmeasured gate must not appear as a pass"
    assert "rvol" not in gates
    assert all_eligible(gates), "remaining measured gates still decide eligibility"


def test_unmeasured_metrics_score_zero_for_their_component() -> None:
    measured = dict(
        rvol=3.0,
        atr_pct=3.5,
        beta=1.5,
        spread_pct=0.0,
        trend_score=100.0,
        catalyst_count=2,
        gap_pct=5.0,
    )
    full = rank_score(**measured)
    without_rvol = rank_score(**{**measured, "rvol": None})
    without_spread = rank_score(**{**measured, "spread_pct": None})
    assert without_rvol == full - 30
    assert without_spread == full - 10


def test_unverified_provider_gates_are_reported_in_warnings_and_filters() -> None:
    class SpreadlessProvider(DemoProvider):
        name = "spreadless"

        async def get_snapshots(self, symbols, as_of, config):
            snapshots, warnings = await super().get_snapshots(symbols, as_of, config)
            marked = [
                replace(snapshot, unverified_gates=("tight_spread",)) for snapshot in snapshots
            ]
            return marked, warnings

    result = asyncio.run(scan_market(SpreadlessProvider(), load_symbols(), ScanConfig()))
    assert result.candidates, "the remaining gates should still qualify names"
    assert all(item.spread_percent is None for item in result.candidates)
    assert all("tight_spread" not in item.passed_filters for item in result.candidates)
    assert "NOT VERIFIED" in result.filters["spread"]
    assert any("unevaluated" in warning for warning in result.warnings)


def _first_snapshot(symbol: str = "AAPL"):
    config = ScanConfig()
    snapshots, _ = asyncio.run(
        DemoProvider().get_snapshots([symbol], datetime.now(UTC), config)
    )
    return snapshots[0], config


def test_pe_ratio_is_derived_from_filed_ttm_eps() -> None:
    snapshot, config = _first_snapshot()
    candidate = _evaluate(snapshot, config, Fundamentals(symbol="AAPL", eps_ttm=4.0))

    assert isinstance(candidate, Candidate)
    assert candidate.pe_ratio == round(candidate.price / 4.0, 2)


def test_pe_ratio_is_omitted_when_earnings_are_not_positive() -> None:
    """A negative P/E is not a cheap stock; rendering one as a number invites
    exactly the wrong reading, so loss-makers report no ratio at all."""
    snapshot, config = _first_snapshot()

    for eps in (-2.89, 0.0, None):
        candidate = _evaluate(snapshot, config, Fundamentals(symbol="AAPL", eps_ttm=eps))
        assert isinstance(candidate, Candidate)
        assert candidate.pe_ratio is None

    # No fundamentals at all is likewise no ratio, never a zero.
    assert _evaluate(snapshot, config, None).pe_ratio is None


def test_reporting_measures_are_present_and_gate_nothing() -> None:
    snapshot, config = _first_snapshot()
    candidate = _evaluate(snapshot, config, None)

    assert isinstance(candidate, Candidate)
    assert candidate.volume_spike is not None and candidate.volume_spike > 0
    assert candidate.rsi is not None and 0 <= candidate.rsi <= 100
    # Neither measure may appear among the hard gates.
    assert "volume_spike" not in candidate.passed_filters
    assert "rsi" not in candidate.passed_filters


def test_volume_confirmation_gates_only_when_configured() -> None:
    """The gate is opt-in, and an unmeasurable ratio never counts as a pass."""
    base = dict(
        config=ScanConfig(),
        price=25.0,
        average_volume=2_000_000,
        spread_pct=0.1,
        atr_pct=3.0,
        beta=1.2,
        rvol=2.0,
        clean_trend=True,
        clear_levels=True,
        has_catalyst=True,
    )
    # Disabled by default: a thinning book does not appear as a gate at all.
    assert "volume_confirmation" not in eligibility(**base, volume_confirmation=0.4)

    demanding = ScanConfig(min_volume_confirmation=1.2)
    assert eligibility(**{**base, "config": demanding}, volume_confirmation=1.25)[
        "volume_confirmation"
    ]
    assert not eligibility(**{**base, "config": demanding}, volume_confirmation=1.19)[
        "volume_confirmation"
    ]
    # Unmeasurable is left unevaluated rather than scored as a pass, matching
    # how every other optional gate behaves.
    assert "volume_confirmation" not in eligibility(
        **{**base, "config": demanding}, volume_confirmation=None
    )


def test_volatility_entry_ignores_the_hard_gates() -> None:
    """A volatile name rejected by the watchlist must still reach the list.

    This is the whole point of the second list: the gates reject volatile names
    for reasons a short-hold trader does not care about, and those names have
    to stay visible without being presented as vetted.
    """
    snapshot, _ = _first_snapshot()
    # Demands nothing can satisfy, so the watchlist path certainly rejects it.
    impossible = ScanConfig(
        require_catalyst=True,
        min_rvol=999.0,
        volatility_min_atr_pct=0.0,
        volatility_min_rvol=0.0,
    )
    assert not isinstance(_evaluate(snapshot, impossible, None), Candidate)

    entry = _volatility_entry(snapshot, impossible)
    assert entry is not None
    assert entry.symbol == snapshot.symbol
    assert entry.volatility_score > 0
    # It carries no trade plan, because it has not earned one.
    assert not hasattr(entry, "thesis")


def test_volatility_entry_enforces_its_own_floor() -> None:
    snapshot, _ = _first_snapshot()
    unreachable_atr = ScanConfig(volatility_min_atr_pct=99.0)
    assert _volatility_entry(snapshot, unreachable_atr) is None

    unreachable_volume = ScanConfig(
        volatility_min_atr_pct=0.0, volatility_min_average_volume=10**12
    )
    assert _volatility_entry(snapshot, unreachable_volume) is None


def test_volatility_list_populates_even_when_nothing_qualifies() -> None:
    """The morning nothing passes is the morning this list matters most."""
    symbols = load_symbols()[:40]
    config = ScanConfig(
        min_rvol=999.0,  # nothing can pass the watchlist
        volatility_min_atr_pct=5.0,
        volatility_min_rvol=0.0,
        use_fundamentals=False,
    )
    result = asyncio.run(scan_market(DemoProvider(), symbols, config))

    assert result.candidates == []
    assert result.volatility_candidates
    assert all(item.atr_percent >= 5.0 for item in result.volatility_candidates)
    # Ranked by volatility score, densest first, and ranks are contiguous.
    scores = [item.volatility_score for item in result.volatility_candidates]
    assert scores == sorted(scores, reverse=True)
    assert [item.rank for item in result.volatility_candidates] == list(
        range(1, len(result.volatility_candidates) + 1)
    )
    assert all(not item.on_watchlist for item in result.volatility_candidates)
    assert any("NOT passed the hard gates" in warning for warning in result.warnings)


def test_volatility_list_can_be_disabled_and_is_capped() -> None:
    symbols = load_symbols()[:40]
    off = ScanConfig(volatility_list_enabled=False, use_fundamentals=False)
    assert asyncio.run(scan_market(DemoProvider(), symbols, off)).volatility_candidates == []

    capped = ScanConfig(
        volatility_list_enabled=True,
        volatility_min_atr_pct=0.0,
        volatility_min_rvol=0.0,
        volatility_list_size=3,
        use_fundamentals=False,
    )
    result = asyncio.run(scan_market(DemoProvider(), symbols, capped))
    assert len(result.volatility_candidates) == 3
