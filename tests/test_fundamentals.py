"""Fundamentals derivation and gating. No test here touches the network."""

from __future__ import annotations

from datetime import UTC, datetime

from market_scanner.fundamentals import Fundamentals, derive_fundamentals, stale_after
from market_scanner.models import ScanConfig
from market_scanner.scoring import fundamental_gates, fundamental_score, rank_score


def _quarters(tag: str, values: list[tuple[str, str, float]]) -> dict:
    return {
        tag: {
            "units": {
                "USD": [
                    {"start": start, "end": end, "val": value, "form": "10-Q"}
                    for start, end, value in values
                ]
            }
        }
    }


def _facts(**tags: dict) -> dict:
    merged: dict = {}
    for group in tags.values():
        merged.update(group)
    return {"facts": {"us-gaap": merged}}


#: Four quarters ending 2025, plus the prior-year comparable for the newest
#: quarter — growth compares Q4 to Q4, so 2024-12-31 must be present.
_REVENUE_2025 = [
    ("2024-01-01", "2024-03-31", 100.0),
    ("2024-10-01", "2024-12-31", 120.0),
    ("2025-01-01", "2025-03-31", 150.0),
    ("2025-04-01", "2025-06-30", 160.0),
    ("2025-07-01", "2025-09-30", 170.0),
    ("2025-10-01", "2025-12-31", 180.0),
]
_INCOME_2025 = [
    ("2025-01-01", "2025-03-31", 15.0),
    ("2025-04-01", "2025-06-30", 16.0),
    ("2025-07-01", "2025-09-30", 17.0),
    ("2025-10-01", "2025-12-31", 18.0),
]


def test_growth_and_margin_from_aligned_quarters() -> None:
    payload = _facts(
        revenue=_quarters("Revenues", _REVENUE_2025),
        income=_quarters("NetIncomeLoss", _INCOME_2025),
    )
    result = derive_fundamentals("TEST", payload)

    assert result.revenue_growth_pct == 50.0  # 180 vs 120 a year earlier
    assert result.revenue_ttm == 660.0  # 150+160+170+180, four most recent quarters
    assert result.net_margin_pct == 10.0  # 66 / 660
    assert result.profitable is True
    assert result.fiscal_period_end == "2025-12-31"


def test_abandoned_tag_never_shadows_the_current_one() -> None:
    """Regression: NVIDIA stopped tagging RevenueFromContractWithCustomer in
    2022 and moved to Revenues. Preference order alone read the dead series and
    reported a four-year-old period as current."""
    payload = _facts(
        stale=_quarters(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            [
                ("2019-01-01", "2019-03-31", 5.0),
                ("2020-01-01", "2020-03-31", 6.0),
            ],
        ),
        current=_quarters("Revenues", _REVENUE_2025),
    )
    result = derive_fundamentals("TEST", payload)

    assert result.fiscal_period_end == "2025-12-31"
    assert result.revenue_ttm == 660.0


def test_widest_current_tag_wins_for_partial_revenue_components() -> None:
    """Regression: SoFi tags a small contract-revenue component alongside the
    real net-of-interest top line. Taking the component inflated net margin."""
    component = [(start, end, value / 8) for start, end, value in _REVENUE_2025]
    payload = _facts(
        component=_quarters("RevenueFromContractWithCustomerExcludingAssessedTax", component),
        total=_quarters("RevenuesNetOfInterestExpense", _REVENUE_2025),
        income=_quarters("NetIncomeLoss", _INCOME_2025),
    )
    result = derive_fundamentals("TEST", payload)

    assert result.revenue_ttm == 660.0
    assert result.net_margin_pct == 10.0


def test_misaligned_windows_yield_no_ratio() -> None:
    """A current net income over a revenue series that stopped years earlier
    produced a 1,240% 'net margin'. Unaligned periods must divide to None."""
    payload = _facts(
        revenue=_quarters(
            "Revenues",
            [
                ("2018-01-01", "2018-03-31", 10.0),
                ("2018-04-01", "2018-06-30", 10.0),
                ("2018-07-01", "2018-09-30", 10.0),
                ("2018-10-01", "2018-12-31", 10.0),
            ],
        ),
        income=_quarters("NetIncomeLoss", _INCOME_2025),
    )
    result = derive_fundamentals("TEST", payload)

    assert result.net_margin_pct is None
    assert "net_margin_pct" in result.missing


def test_growth_off_a_non_positive_base_is_not_reported() -> None:
    payload = _facts(
        revenue=_quarters(
            "Revenues",
            [("2024-01-01", "2024-03-31", 0.0), ("2025-01-01", "2025-03-31", 150.0)],
        )
    )
    assert derive_fundamentals("TEST", payload).revenue_growth_pct is None


def test_empty_filings_report_everything_missing() -> None:
    result = derive_fundamentals("TEST", {"facts": {"us-gaap": {}}})

    assert result.coverage == 0.0
    assert result.revenue_growth_pct is None
    assert result.profitable is None
    assert result.note


def test_unmeasurable_metrics_leave_their_gates_unevaluated() -> None:
    config = ScanConfig(use_fundamentals=True, min_revenue_growth_pct=10.0)
    empty = Fundamentals(symbol="TEST", missing=("revenue_growth_pct",))

    gates = fundamental_gates(config=config, fundamentals=empty)

    # Absent evidence must neither pass nor fail the gate.
    assert "revenue_growth" not in gates
    assert gates == {}


def test_gates_reject_only_on_filed_evidence() -> None:
    config = ScanConfig(use_fundamentals=True, min_revenue_growth_pct=10.0, max_debt_to_equity=1.0)
    weak = Fundamentals(symbol="TEST", revenue_growth_pct=4.0, debt_to_equity=2.5)

    gates = fundamental_gates(config=config, fundamentals=weak)

    assert gates["revenue_growth"] is False
    assert gates["debt_to_equity"] is False


def test_require_fundamentals_rejects_unfiled_names() -> None:
    config = ScanConfig(use_fundamentals=True, require_fundamentals=True)
    gates = fundamental_gates(config=config, fundamentals=None)

    assert gates["fundamentals_available"] is False


def test_disabled_fundamentals_add_no_gates() -> None:
    strong = Fundamentals(symbol="TEST", revenue_growth_pct=90.0)
    assert fundamental_gates(config=ScanConfig(), fundamentals=strong) == {}


def test_score_rescales_across_measurable_components_only() -> None:
    """Partial coverage must not be scored as weakness: a company with only a
    strong growth figure filed should not be dragged toward zero by absent
    margin and leverage metrics."""
    partial = Fundamentals(symbol="TEST", revenue_growth_pct=40.0)
    full = Fundamentals(
        symbol="TEST",
        revenue_growth_pct=40.0,
        eps_growth_pct=50.0,
        net_margin_pct=25.0,
        return_on_equity_pct=30.0,
        debt_to_equity=0.0,
    )

    assert fundamental_score(partial) == 100.0
    assert fundamental_score(full) == 100.0
    assert fundamental_score(Fundamentals(symbol="TEST")) is None


def test_blended_rank_moves_with_fundamentals() -> None:
    technical = dict(
        rvol=2.0,
        atr_pct=3.5,
        beta=1.5,
        spread_pct=0.1,
        trend_score=80.0,
        catalyst_count=2,
        gap_pct=3.0,
    )
    baseline = rank_score(**technical)
    strong = rank_score(**technical, fundamental=100.0)
    weak = rank_score(**technical, fundamental=0.0)

    assert weak < baseline < strong
    # No fundamental score must leave the technical ranking untouched.
    assert rank_score(**technical, fundamental=None) == baseline


def test_stale_filings_are_detected_against_the_run_date() -> None:
    fresh = Fundamentals(symbol="TEST", fiscal_period_end="2025-12-31")
    now = datetime(2026, 3, 1, tzinfo=UTC)

    assert stale_after(fresh, now, max_age_days=200) is False
    assert stale_after(fresh, now, max_age_days=30) is True
    # Unknown period ends cannot be judged stale.
    assert stale_after(Fundamentals(symbol="TEST"), now, max_age_days=1) is False
