from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_scanner.blowing_reporting import (
    BASENAME,
    prune_history,
    render_csv,
    render_html,
    render_json,
    render_markdown,
    write_reports,
)
from market_scanner.blowing_sources import _window_volumes, session_phase
from market_scanner.blowing_stocks import (
    BlowingSnapshot,
    BlowingStocksConfig,
    BlowingStocksScreener,
    CatalystCriteria,
    LowFloatCriteria,
    config_from_mapping,
    matched_keywords,
)
from market_scanner.float_data import FloatProvider, _RawFacts, load_overrides
from market_scanner.models import Bar

EASTERN = ZoneInfo("America/New_York")
SESSION = date(2026, 8, 19)


def snapshot(**overrides) -> BlowingSnapshot:
    """A symbol that clears every gate on both screens, before overrides."""
    base = {
        "symbol": "TEST",
        "price": 3.00,
        "session_date": SESSION,
        "data_as_of": datetime(2026, 8, 19, 13, 0, tzinfo=UTC),
        "previous_close": 2.00,
        "previous_high": 2.40,
        "sma": 2.10,
        "sma_period": 50,
        "average_volume": 300_000,
        "session_volume": 900_000,
        "premarket_volume": 900_000,
        "rvol": 12.0,
        "premarket_high": 2.90,
        "premarket_high_basis": "test",
        "vwap": 2.75,
        "vwap_anchor": "test",
        "float_shares": 4_000_000,
        "float_basis": "test",
        "float_as_of": SESSION.isoformat(),
        "headlines": ("Company wins FDA clearance for its AI platform",),
        "session_phase": "regular",
    }
    return BlowingSnapshot(**{**base, **overrides})


# ------------------------------------------------------------------ keywords


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("XYZ receives FDA breakthrough designation", ["FDA"]),
        ("XYZ granted US patent for its device", ["Patent"]),
        ("XYZ announces 1-for-10 reverse stock split", ["Reverse split"]),
        ("XYZ beats estimates on record quarter", ["Earnings beat"]),
        ("XYZ launches artificial intelligence platform", ["AI"]),
        ("XYZ enters strategic partnership with Acme", ["Partnership"]),
    ],
)
def test_each_configured_keyword_matches_the_wire_phrasing(headline, expected) -> None:
    """Headlines rarely use the config's exact words; the patterns allow for it."""
    assert matched_keywords([headline], LowFloatCriteria().keywords) == expected


@pytest.mark.parametrize(
    "headline",
    [
        "Chairman said the quarter was solid",
        "Company will maintain its dividend",
        "Air cargo volumes rose again",
        "Retailer plans to remain independent",
    ],
)
def test_ai_does_not_match_ordinary_words_containing_those_letters(headline) -> None:
    """A substring search for "AI" hits said, maintain, Air and remain.

    The AI pattern is matched case-sensitively for exactly this reason; a false
    catalyst is worse than a missed one, because it is indistinguishable from
    a real one on the report.
    """
    assert matched_keywords([headline], ("AI",)) == []


def test_phase_two_and_three_match_both_arabic_and_roman_numerals() -> None:
    keywords = CatalystCriteria().keywords
    assert matched_keywords(["Topline Phase III data readout"], keywords) == ["Phase 3"]
    assert matched_keywords(["Initiates phase 2 trial"], keywords) == ["Phase 2"]


def test_keywords_are_returned_in_configuration_order_not_headline_order() -> None:
    headline = "Acme announces AI partnership and FDA clearance"
    assert matched_keywords([headline], LowFloatCriteria().keywords) == [
        "FDA",
        "AI",
        "Partnership",
    ]


# ---------------------------------------------------------------- screen A


def test_a_qualifying_symbol_clears_every_low_float_gate() -> None:
    result = BlowingStocksScreener().screen_low_float(snapshot())
    assert not isinstance(result, list)
    assert result.screen == "low_float"
    assert set(result.passed_filters) == {
        "price",
        "float",
        "rvol",
        "gap",
        "volume",
        "news",
        "above_premarket_high",
        "above_vwap",
    }
    assert all(result.passed_filters.values())
    assert result.keywords == ["FDA", "AI"]


@pytest.mark.parametrize(
    ("field", "value", "gate"),
    [
        ("price", 0.40, "price"),
        ("price", 25.0, "price"),
        ("float_shares", 30_000_000, "float"),
        ("rvol", 4.0, "rvol"),
        ("previous_close", 2.90, "gap"),
        ("session_volume", 400_000, "volume"),
        ("headlines", (), "news"),
        ("premarket_high", 3.50, "above_premarket_high"),
        ("vwap", 3.50, "above_vwap"),
    ],
)
def test_each_low_float_gate_rejects_on_its_own(field, value, gate) -> None:
    outcome = BlowingStocksScreener().screen_low_float(snapshot(**{field: value}))
    assert isinstance(outcome, list)
    assert gate in outcome


def test_the_volume_floor_follows_the_session_phase() -> None:
    """500K is a full session's bar; before the open the brief asks for 200K.

    Applying the session floor to a premarket run would reject every name for
    not yet having traded volume the session has not had time to produce.
    """
    premarket = snapshot(
        session_phase="premarket", session_volume=250_000, premarket_volume=250_000
    )
    assert not isinstance(BlowingStocksScreener().screen_low_float(premarket), list)
    regular = replace(premarket, session_phase="regular")
    assert "volume" in BlowingStocksScreener().screen_low_float(regular)


# ---------------------------------------------------------- unmeasured gates


@pytest.mark.parametrize(
    ("field", "gate"),
    [
        ("rvol", "rvol_unproven"),
        ("previous_close", "gap_unproven"),
        ("session_volume", "volume_unproven"),
        ("premarket_high", "above_premarket_high_unproven"),
        ("vwap", "above_vwap_unproven"),
    ],
)
def test_an_unmeasured_gate_rejects_rather_than_passing(field, gate) -> None:
    """The screener's whole claim is that these conditions held this morning.

    A row that cannot say what its RVOL or gap was is not weak evidence, it is
    no evidence, so it never reaches the report.
    """
    outcome = BlowingStocksScreener().screen_low_float(snapshot(**{field: None}))
    assert isinstance(outcome, list)
    assert gate in outcome


def test_unmeasured_news_is_not_read_as_an_absence_of_news() -> None:
    outcome = BlowingStocksScreener().screen_low_float(
        snapshot(news_available=False, headlines=())
    )
    assert "news_unproven" in outcome
    assert "news" not in outcome


def test_float_is_the_one_gate_the_operator_may_leave_unproven() -> None:
    unknown = snapshot(float_shares=None, float_basis=None, float_as_of=None)
    assert "float_unproven" in BlowingStocksScreener().screen_low_float(unknown)

    relaxed = BlowingStocksConfig(require_float=False)
    candidate = BlowingStocksScreener(relaxed).screen_low_float(unknown)
    assert not isinstance(candidate, list)
    # It is kept, but the unproven gate travels with the row so no reader can
    # mistake "not measured" for "measured and passed".
    assert candidate.unproven == ["float"]
    assert "float" not in candidate.passed_filters
    assert candidate.float_shares is None


def test_a_stale_filed_float_is_treated_as_unknown_not_as_current() -> None:
    """Float moves on offerings; a cover page from two years ago is not evidence."""
    stale = snapshot(float_as_of=(SESSION - timedelta(days=800)).isoformat())
    assert "float_unproven" in BlowingStocksScreener().screen_low_float(stale)


def test_an_upper_bound_float_that_passes_is_still_a_genuine_pass() -> None:
    """Float cannot exceed shares outstanding, so passing on that basis is proof."""
    bounded = snapshot(float_shares=9_000_000, float_is_upper_bound=True)
    candidate = BlowingStocksScreener().screen_low_float(bounded)
    assert not isinstance(candidate, list)
    assert candidate.float_is_upper_bound is True


# ------------------------------------------------------------------- tiers


def test_the_weakest_dimension_names_the_tier() -> None:
    """A 3M float at RVOL 6 is a good setup, but it is not parabolic.

    Letting the strongest axis name the row would print "parabolic" beside a
    stock that is not moving parabolically.
    """
    tiny_float_slow_tape = snapshot(float_shares=2_000_000, rvol=6.0, previous_close=2.0)
    candidate = BlowingStocksScreener().screen_low_float(tiny_float_slow_tape)
    # The gap ladder has only the two rungs the brief gave it — the gate and
    # "best" — so its top two tiers share a threshold rather than inventing a
    # number that was never specified.
    assert candidate.tier_detail == {
        "float": "parabolic",
        "rvol": "standard",
        "gap": "parabolic",
    }
    assert candidate.tier == "standard"


def test_every_dimension_at_its_top_tier_reads_parabolic() -> None:
    candidate = BlowingStocksScreener().screen_low_float(
        snapshot(float_shares=2_000_000, rvol=25.0, previous_close=2.0, price=3.0)
    )
    assert candidate.tier == "parabolic"
    assert candidate.score > 80


# ---------------------------------------------------------------- screen B


def test_the_catalyst_screen_requires_a_keyword_not_merely_news() -> None:
    """Screen B is defined by its catalyst, so unrelated news is not a pass."""
    unrelated = snapshot(headlines=("Company appoints new chief marketing officer",))
    assert "keyword" in BlowingStocksScreener().screen_catalyst(unrelated)


@pytest.mark.parametrize(
    ("field", "value", "gate"),
    [
        ("price", 0.80, "price"),
        ("price", 60.0, "price"),
        ("rvol", 2.0, "rvol"),
        ("average_volume", 600_000, "volume_surge"),
        ("previous_high", 3.50, "above_previous_high"),
        ("sma", 3.50, "above_sma"),
    ],
)
def test_each_catalyst_gate_rejects_on_its_own(field, value, gate) -> None:
    outcome = BlowingStocksScreener().screen_catalyst(snapshot(**{field: value}))
    assert isinstance(outcome, list)
    assert gate in outcome


def test_the_volume_surge_is_measured_against_the_twenty_session_mean() -> None:
    candidate = BlowingStocksScreener().screen_catalyst(
        snapshot(session_volume=900_000, average_volume=300_000)
    )
    assert candidate.volume_multiple == 3.0


# -------------------------------------------------------------------- run


def test_a_symbol_can_appear_on_both_screens_and_is_counted_once() -> None:
    result = BlowingStocksScreener().run(
        [snapshot()], provider="test", generated_at="now", session_date=SESSION
    )
    assert [item.symbol for item in result.low_float] == ["TEST"]
    assert [item.symbol for item in result.catalyst] == ["TEST"]
    assert result.symbols_qualified == 1


def test_results_are_ranked_by_score_and_never_padded() -> None:
    strong = snapshot(symbol="AAA", float_shares=1_000_000, rvol=25.0)
    weak = snapshot(symbol="BBB", float_shares=19_000_000, rvol=5.5)
    rejected = snapshot(symbol="CCC", price=0.10)
    result = BlowingStocksScreener().run(
        [weak, strong, rejected], provider="test", generated_at="now", session_date=SESSION
    )
    assert [item.symbol for item in result.low_float] == ["AAA", "BBB"]
    assert [item.rank for item in result.low_float] == [1, 2]
    assert result.rejection_counts["low_float"]["price"] == 1


def test_the_result_cap_is_a_ceiling_not_a_target() -> None:
    config = BlowingStocksConfig(max_results=2)
    everyone = [snapshot(symbol=f"S{index}", rvol=10.0 + index) for index in range(5)]
    result = BlowingStocksScreener(config).run(
        everyone, provider="test", generated_at="now", session_date=SESSION
    )
    assert len(result.low_float) == 2
    assert [item.symbol for item in result.low_float] == ["S4", "S3"]


def test_relaxing_the_float_gate_is_announced_in_the_warnings() -> None:
    result = BlowingStocksScreener(BlowingStocksConfig(require_float=False)).run(
        [snapshot(float_shares=None)],
        provider="test",
        generated_at="now",
        session_date=SESSION,
    )
    assert any("require_float is off" in warning for warning in result.warnings)


# ----------------------------------------------------------------- config


def test_the_toml_section_builds_the_nested_screens() -> None:
    config = config_from_mapping(
        {
            "max_results": 5,
            "low_float": {"min_rvol": 7.0, "keywords": ["FDA", "Merger"]},
            "catalyst": {"sma_period": 20},
        }
    )
    assert config.max_results == 5
    assert config.low_float.min_rvol == 7.0
    assert config.low_float.keywords == ("FDA", "Merger")
    assert config.catalyst.sma_period == 20


@pytest.mark.parametrize(
    "payload",
    [
        {"max_reslts": 5},
        {"low_float": {"min_rvl": 7.0}},
        {"catalyst": {"smaperiod": 20}},
    ],
)
def test_a_mistyped_config_key_fails_loudly(payload) -> None:
    """A silently ignored key is a gate the operator believes is applied."""
    with pytest.raises(ValueError, match="Unknown"):
        config_from_mapping(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"low_float": {"min_price": 30.0, "max_price": 20.0}},
        {"low_float": {"parabolic_float": 30_000_000}},
        {"low_float": {"min_rvol": 30.0}},
        {"catalyst": {"min_volume_multiple": 0}},
        {"history_retention_days": 0},
        {"catalyst": {"sma_period": 200}},
    ],
)
def test_incoherent_thresholds_are_rejected_at_startup(payload) -> None:
    with pytest.raises(ValueError):
        config_from_mapping(payload)


def test_the_shipped_configuration_matches_the_brief() -> None:
    """The numbers in config/scanner.toml are the ones that were asked for."""
    from market_scanner.config import load_blowing_stocks_config

    config = load_blowing_stocks_config()
    low, catalyst = config.low_float, config.catalyst
    assert (low.min_price, low.max_price) == (0.50, 20.0)
    assert (low.max_float, low.best_float, low.parabolic_float) == (2e7, 1e7, 5e6)
    assert (low.min_rvol, low.explosive_rvol, low.parabolic_rvol) == (5.0, 10.0, 20.0)
    assert (low.min_gap_pct, low.best_gap_pct) == (20.0, 30.0)
    assert (low.min_volume, low.min_premarket_volume) == (500_000, 200_000)
    assert low.require_news and low.require_above_premarket_high and low.require_above_vwap
    assert (catalyst.min_price, catalyst.max_price) == (1.0, 50.0)
    assert catalyst.min_rvol == 3.0
    assert catalyst.min_volume_multiple == 2.0
    assert catalyst.require_above_previous_high and catalyst.require_above_sma
    assert catalyst.sma_period == 50
    assert config.history_retention_days == 7


# ------------------------------------------------------------ measurement


def _bars(times_and_volumes, day: date = SESSION, price: float = 1.0) -> list[Bar]:
    return [
        Bar(
            datetime.combine(day, clock, tzinfo=EASTERN),
            price,
            high,
            price * 0.9,
            close,
            volume,
        )
        for clock, high, close, volume in times_and_volumes
    ]


def test_the_premarket_high_excludes_the_current_bar_before_the_bell() -> None:
    """Inside the premarket the high is still running and contains the price.

    Comparing a price against a high it is itself part of can only ever be
    false, which would make the breakout gate reject every symbol forever. The
    level therefore stops at the previous completed bar, so the comparison
    means "making new premarket highs right now" — what the condition asks.
    """
    from datetime import time as clock_time

    bars = _bars(
        [
            (clock_time(7, 0), 1.10, 1.05, 1_000),
            (clock_time(7, 5), 1.20, 1.18, 2_000),
            (clock_time(7, 10), 1.40, 1.38, 5_000),
        ]
    )
    cutoff = datetime.combine(SESSION, clock_time(7, 15), tzinfo=EASTERN)
    window, _, _ = _window_volumes(bars, SESSION, cutoff)
    assert window.premarket_high == 1.20
    assert window.close == 1.38
    assert window.close > window.premarket_high  # a genuine breakout


def test_after_the_bell_the_premarket_high_is_the_settled_level() -> None:
    from datetime import time as clock_time

    bars = _bars(
        [
            (clock_time(7, 0), 1.10, 1.05, 1_000),
            (clock_time(9, 25), 1.40, 1.38, 5_000),
            (clock_time(9, 35), 1.30, 1.25, 9_000),
        ]
    )
    cutoff = datetime.combine(SESSION, clock_time(9, 45), tzinfo=EASTERN)
    window, _, _ = _window_volumes(bars, SESSION, cutoff)
    assert window.premarket_high == 1.40
    assert window.premarket_volume == 6_000
    assert window.volume == 15_000
    assert window.vwap_anchor.startswith("regular session")


def test_relative_volume_compares_the_same_clock_window_on_both_sides() -> None:
    """An RVOL that measured a partial window against full prior sessions would
    read low for every symbol and quietly gut a gate set at 5."""
    from datetime import time as clock_time

    bars = []
    # Trading days only: the session before this one is a Wednesday, so the
    # three prior sessions skip the intervening weekend.
    for offset, volume in ((5, 1_000), (2, 1_000), (1, 1_000)):
        day = SESSION - timedelta(days=offset)
        bars.extend(_bars([(clock_time(7, 0), 1.0, 1.0, volume)], day=day))
        # Volume after the cutoff must not enter the baseline.
        bars.extend(_bars([(clock_time(11, 0), 1.0, 1.0, 500_000)], day=day))
    bars.extend(_bars([(clock_time(7, 0), 1.0, 1.0, 8_000)]))
    cutoff = datetime.combine(SESSION, clock_time(8, 0), tzinfo=EASTERN)
    window, baseline, had_bars = _window_volumes(bars, SESSION, cutoff)
    assert had_bars
    assert baseline == [1_000, 1_000, 1_000]
    assert window.volume / (sum(baseline) / len(baseline)) == 8.0


def test_a_symbol_that_did_not_trade_is_zero_not_unmeasured() -> None:
    """A measured absence and a missing measurement are different facts."""
    from datetime import time as clock_time

    prior_only = _bars([(clock_time(7, 0), 1.0, 1.0, 5_000)], day=SESSION - timedelta(days=1))
    window, baseline, had_bars = _window_volumes(
        prior_only, SESSION, datetime.combine(SESSION, clock_time(8, 0), tzinfo=EASTERN)
    )
    assert window is None and had_bars is True and baseline == [5_000]

    window, baseline, had_bars = _window_volumes(
        [], SESSION, datetime.combine(SESSION, clock_time(8, 0), tzinfo=EASTERN)
    )
    assert window is None and had_bars is False


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(3, 0, "closed"), (7, 30, "premarket"), (10, 0, "regular"), (17, 0, "after-hours")],
)
def test_the_session_phase_is_named_from_the_measurement_cutoff(hour, minute, expected) -> None:
    from datetime import time as clock_time

    cutoff = datetime.combine(SESSION, clock_time(hour, minute), tzinfo=EASTERN)
    assert session_phase(cutoff, SESSION) == expected


def test_a_cutoff_on_another_day_is_reported_closed() -> None:
    cutoff = datetime(2026, 8, 22, 10, 0, tzinfo=EASTERN)
    assert session_phase(cutoff, SESSION) == "closed"


# ------------------------------------------------------------------ float


def test_public_float_is_converted_with_the_split_adjusted_close() -> None:
    """A reverse split between the filing and today would otherwise throw the
    share count off by exactly the split ratio."""
    provider = FloatProvider(today=SESSION)
    facts = _RawFacts(public_float_usd=10_000_000, public_float_as_of="2026-06-30")
    fact = provider._derive("TEST", facts, lambda symbol, on: (4.0, date(2026, 6, 30)))
    assert fact.shares == 2_500_000
    assert fact.is_upper_bound is False
    assert "public float" in fact.basis


def test_a_stale_public_float_defers_to_a_fresh_share_count() -> None:
    """The two facts age at very different rates. Discarding a symbol because
    the better fact went stale, while a fresh upper bound sat beside it, would
    reject exactly the names this screener exists to find."""
    provider = FloatProvider(today=SESSION, max_age_days=400)
    facts = _RawFacts(
        public_float_usd=10_000_000,
        public_float_as_of="2023-06-30",
        shares_outstanding=7_000_000,
        shares_outstanding_as_of="2026-07-31",
    )
    fact = provider._derive("TEST", facts, lambda symbol, on: (4.0, date(2023, 6, 30)))
    assert fact.shares == 7_000_000
    assert fact.is_upper_bound is True


def test_float_is_never_reported_above_shares_outstanding() -> None:
    provider = FloatProvider(today=SESSION)
    facts = _RawFacts(
        public_float_usd=10_000_000,
        public_float_as_of="2026-06-30",
        shares_outstanding=1_000_000,
        shares_outstanding_as_of="2026-07-31",
    )
    fact = provider._derive("TEST", facts, lambda symbol, on: (4.0, date(2026, 6, 30)))
    assert fact.shares == 1_000_000
    assert fact.is_upper_bound is True


def test_heavy_dilution_since_the_float_was_measured_is_flagged() -> None:
    provider = FloatProvider(today=SESSION)
    facts = _RawFacts(
        public_float_usd=10_000_000,
        public_float_as_of="2026-06-30",
        shares_outstanding=20_000_000,
        shares_outstanding_as_of="2026-07-31",
    )
    fact = provider._derive("TEST", facts, lambda symbol, on: (4.0, date(2026, 6, 30)))
    assert fact.shares == 2_500_000
    assert "8.0x the derived float" in fact.note


def test_a_price_far_from_the_measurement_date_is_refused() -> None:
    """Pricing a float off a close from a different month is not a measurement."""
    provider = FloatProvider(today=SESSION)
    facts = _RawFacts(public_float_usd=10_000_000, public_float_as_of="2026-06-30")
    fact = provider._derive("TEST", facts, lambda symbol, on: (4.0, date(2026, 1, 5)))
    assert fact.shares is None
    assert "within a week" in fact.note


def test_no_filed_facts_leaves_the_float_unknown_rather_than_zero() -> None:
    fact = FloatProvider(today=SESSION)._derive("TEST", _RawFacts(), None)
    assert fact.shares is None
    assert fact.is_upper_bound is False


def test_operator_overrides_parse_around_comments_and_bad_rows(tmp_path: Path) -> None:
    path = tmp_path / "float_overrides.csv"
    path.write_text(
        "# a comment the file uses to explain itself\n"
        "symbol,float_shares,as_of,source\n"
        "aaa,1500000,2026-08-01,filing cover page\n"
        "BBB,not-a-number,,\n"
        "CCC,0,,\n"
        "DDD,2_400_000,,\n",
        encoding="utf-8",
    )
    overrides = load_overrides(path)
    assert set(overrides) == {"AAA", "DDD"}
    assert overrides["AAA"].shares == 1_500_000
    assert overrides["AAA"].is_upper_bound is False
    assert "filing cover page" in overrides["AAA"].basis
    assert overrides["DDD"].shares == 2_400_000


def test_a_missing_override_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_overrides(tmp_path / "absent.csv") == {}
    assert load_overrides(None) == {}


# -------------------------------------------------------------- reporting


@pytest.fixture
def result():
    return BlowingStocksScreener().run(
        [snapshot(symbol="AAA"), snapshot(symbol="BBB", price=0.10)],
        provider="test",
        generated_at="2026-08-19T13:33:00+00:00",
        session_date=SESSION,
        feed="delayed_sip",
        session_phase="premarket",
        universe_size=6_243,
    )


def test_every_renderer_produces_the_row_and_the_disclaimer(result) -> None:
    payload = json.loads(render_json(result))
    assert payload["report"] == "blowing-stocks"
    assert payload["low_float"][0]["symbol"] == "AAA"
    assert "not investment advice" in payload["disclaimer"].lower()

    csv_text = render_csv(result)
    assert "screen,rank,symbol" in csv_text
    assert "low_float,1,AAA" in csv_text

    markdown = render_markdown(result)
    assert "**AAA**" in markdown and "Not investment advice" in markdown

    page = render_html(result)
    assert "AAA" in page and "<!doctype html>" in page


def test_an_empty_screen_says_so_instead_of_rendering_a_blank_table() -> None:
    empty = BlowingStocksScreener().run(
        [], provider="test", generated_at="now", session_date=SESSION
    )
    assert "not padded" in render_markdown(empty)
    assert "not padded" in render_html(empty)


def test_unmeasured_values_render_as_a_dash_never_as_zero() -> None:
    relaxed = BlowingStocksConfig(require_float=False)
    result = BlowingStocksScreener(relaxed).run(
        [snapshot(float_shares=None)],
        provider="test",
        generated_at="now",
        session_date=SESSION,
    )
    row = render_markdown(result).splitlines()
    line = next(line for line in row if "**TEST**" in line)
    assert "| — |" in line


def test_headlines_are_escaped_into_the_html(result) -> None:
    injected = BlowingStocksScreener().run(
        [snapshot(headlines=("FDA nod <script>alert(1)</script>",))],
        provider="test",
        generated_at="now",
        session_date=SESSION,
    )
    page = render_html(injected)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# ------------------------------------------------------------- retention


def test_a_run_writes_the_current_report_and_a_dated_copy(result, tmp_path: Path) -> None:
    written = write_reports(result, tmp_path, retention_days=7, run_date=SESSION)
    for name in ("json", "csv", "markdown", "html"):
        assert written["current"][name].exists()
        assert written["archived"][name].exists()
    assert written["archived"]["json"].name == f"{BASENAME}-2026-08-19.json"
    assert written["archived"]["json"].parent.name == "history"


def test_the_archive_keeps_exactly_the_retention_window(tmp_path: Path) -> None:
    for offset in range(0, 12):
        stamp = (SESSION - timedelta(days=offset)).isoformat()
        (tmp_path / f"{BASENAME}-{stamp}.json").write_text("{}", encoding="utf-8")

    removed = prune_history(tmp_path, SESSION, retention_days=7)
    kept = sorted(path.name for path in tmp_path.iterdir())

    assert len(kept) == 7
    assert kept[0] == f"{BASENAME}-2026-08-13.json"
    assert kept[-1] == f"{BASENAME}-2026-08-19.json"
    assert len(removed) == 5


def test_pruning_only_ever_touches_files_this_module_wrote(tmp_path: Path) -> None:
    """The archive lives in the operator's artifacts directory. A pattern that
    swept up anything else would delete their files, not ours."""
    keep = [
        "notes.txt",
        "blowing-stocks.json",
        "blowing-stocks-2020-01-01.txt",
        "other-2020-01-01.json",
        "blowing-stocks-not-a-date.json",
    ]
    for name in keep:
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / f"{BASENAME}-2020-01-01.json").write_text("{}", encoding="utf-8")

    removed = prune_history(tmp_path, SESSION, retention_days=7)

    assert [path.name for path in removed] == [f"{BASENAME}-2020-01-01.json"]
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(keep)


def test_pruning_a_directory_that_does_not_exist_is_harmless(tmp_path: Path) -> None:
    assert prune_history(tmp_path / "absent", SESSION, retention_days=7) == []


def test_a_rerun_on_the_same_day_replaces_that_day_rather_than_accumulating(
    result, tmp_path: Path
) -> None:
    write_reports(result, tmp_path, retention_days=7, run_date=SESSION)
    write_reports(result, tmp_path, retention_days=7, run_date=SESSION)
    archive = tmp_path / "history"
    assert len(list(archive.glob(f"{BASENAME}-*.json"))) == 1


def test_the_archive_is_stamped_with_the_run_date_not_the_session_date(
    tmp_path: Path,
) -> None:
    """A Saturday run reports Friday's session. Stamping it Friday would
    overwrite Friday's own report with a market-closed page."""
    closed = BlowingStocksScreener().run(
        [], provider="test", generated_at="now", session_date=SESSION, session_phase="closed"
    )
    write_reports(closed, tmp_path, retention_days=7, run_date=date(2026, 8, 22))
    assert (tmp_path / "history" / f"{BASENAME}-2026-08-22.json").exists()
    assert not (tmp_path / "history" / f"{BASENAME}-2026-08-19.json").exists()
