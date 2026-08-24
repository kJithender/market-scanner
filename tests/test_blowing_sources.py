"""Collector tests for the BlowingStocksScreener, against a stubbed feed.

The collector is the riskiest code in the screener: it is where a wrong
timezone, a wrong feed name, or a window that closes at a different moment on
one side of a ratio turns into a number that looks plausible and is wrong. So
the HTTP layer is stubbed and the arithmetic is checked against payloads
shaped exactly like Alpaca's.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_scanner import blowing_sources
from market_scanner.blowing_sources import (
    _BARS_FEED,
    AlpacaBlowingSource,
    DemoBlowingSource,
    discover_universe,
)
from market_scanner.blowing_stocks import BlowingStocksConfig, BlowingStocksScreener
from market_scanner.float_data import FloatFact, FloatProvider
from market_scanner.providers.alpaca import ProviderError

EASTERN = ZoneInfo("America/New_York")
SESSION = date(2026, 8, 19)
CONFIG = BlowingStocksConfig()


def _stamp(clock: time, day: date = SESSION) -> str:
    return (
        datetime.combine(day, clock, tzinfo=EASTERN)
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _daily(day: date, close: float, high: float, volume: int) -> dict:
    return {
        "t": _stamp(time(0, 0), day),
        "o": close,
        "h": high,
        "l": close * 0.9,
        "c": close,
        "v": volume,
    }


def _minute(clock: time, close: float, high: float, volume: int, day: date = SESSION) -> dict:
    return {
        "t": _stamp(clock, day),
        "o": close,
        "h": high,
        "l": close * 0.95,
        "c": close,
        "v": volume,
    }


class StubSource(AlpacaBlowingSource):
    """An Alpaca collector whose HTTP layer answers from canned payloads."""

    def __init__(self, responses: dict, **kwargs) -> None:
        super().__init__(key_id="key", secret="secret", **kwargs)
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def _get(self, path: str, params: dict) -> dict:
        self.calls.append((path, params))
        payload = self.responses.get(path)
        if isinstance(payload, Exception):
            raise payload
        if callable(payload):
            return payload(params)
        if payload is None:
            raise ProviderError(f"no stub for {path}")
        if path == "/v2/stocks/snapshots":
            # The real endpoint answers only for what was asked for.
            wanted = set(params["symbols"].split(","))
            return {symbol: snap for symbol, snap in payload.items() if symbol in wanted}
        return payload


class StubFloats:
    """A float provider that answers without touching EDGAR."""

    def __init__(self, facts: dict[str, FloatFact], warnings: list[str] | None = None) -> None:
        self.facts = facts
        self.warnings = warnings or []
        self.priced: list[tuple[str, date]] = []

    def fetch(self, symbols, prices=None):
        if prices is not None:
            for symbol in symbols:
                found = prices(symbol, date(2026, 6, 30))
                self.priced.append((symbol, found[1] if found else None))
        return {symbol: self.facts.get(symbol, FloatFact(symbol=symbol)) for symbol in symbols}, (
            self.warnings
        )


def build_source(**kwargs) -> StubSource:
    """A collector holding one gapping low-float name and one quiet one."""
    prior_days = [SESSION - timedelta(days=offset) for offset in (1, 2, 5, 6, 7)]
    responses = {
        "/v2/stocks/snapshots": {
            "HOT": {
                "dailyBar": _daily(SESSION, 3.00, 3.10, 900_000),
                "prevDailyBar": _daily(prior_days[0], 2.00, 2.40, 120_000),
                "latestTrade": {"p": 3.05, "t": _stamp(time(9, 17))},
                "minuteBar": _minute(time(9, 15), 3.02, 3.05, 4_000),
            },
            "QUIET": {
                "dailyBar": _daily(SESSION, 5.00, 5.05, 60_000),
                "prevDailyBar": _daily(prior_days[0], 4.99, 5.02, 58_000),
                "latestTrade": {"p": 5.00, "t": _stamp(time(9, 17))},
                "minuteBar": _minute(time(9, 15), 5.00, 5.01, 100),
            },
            "PRICEY": {
                "dailyBar": _daily(SESSION, 900.0, 950.0, 900_000),
                "prevDailyBar": _daily(prior_days[0], 500.0, 520.0, 120_000),
                "latestTrade": {"p": 900.0, "t": _stamp(time(9, 17))},
                "minuteBar": _minute(time(9, 15), 900.0, 905.0, 4_000),
            },
        },
        "/v1beta1/news": {
            "news": [
                {
                    "headline": "HOT wins FDA clearance and announces AI partnership",
                    "symbols": ["HOT"],
                    "created_at": _stamp(time(6, 0)),
                    "source": "wire",
                    "url": "https://example.invalid/hot",
                }
            ],
            "next_page_token": None,
        },
    }

    def bars(params: dict) -> dict:
        if params["timeframe"] == "1Day":
            history = {
                symbol: [
                    _daily(SESSION - timedelta(days=offset), 2.00, 2.40, 100_000)
                    for offset in range(120, 0, -1)
                ]
                + [_daily(SESSION, 3.00, 3.10, 900_000)]
                for symbol in params["symbols"].split(",")
            }
            return {"bars": history, "next_page_token": None}
        intraday = {}
        for symbol in params["symbols"].split(","):
            rows = [
                _minute(time(7, 0), 1.9, 2.0, 20_000, day=day)
                for day in prior_days
                if day.weekday() < 5
            ]
            rows += [
                _minute(time(7, 0), 2.50, 2.60, 300_000),
                _minute(time(8, 0), 2.80, 2.85, 400_000),
                _minute(time(9, 0), 3.05, 3.10, 500_000),
                # After the cutoff: must not enter any measurement.
                _minute(time(10, 0), 9.99, 9.99, 9_000_000),
            ]
            intraday[symbol] = rows
        return {"bars": intraday, "next_page_token": None}

    responses["/v2/stocks/bars"] = bars
    return StubSource(responses, **kwargs)


AS_OF = datetime.combine(SESSION, time(9, 30), tzinfo=EASTERN)  # cutoff 09:15 after the delay


# ------------------------------------------------------------------- feeds


def test_the_bars_endpoint_gets_the_feed_name_it_actually_accepts() -> None:
    """``delayed_sip`` is a snapshot feed; the bars endpoint answers 400 for it
    and spells the same entitlement ``sip``. Sending ``iex`` bars instead would
    not be a smaller version of the same data — IEX prints so little premarket
    that the five-minute bars come back empty, silently zeroing the premarket
    volume, high, VWAP and RVOL that screen A is built on."""
    source = build_source(feed="delayed_sip")
    assert source.feed == "delayed_sip"
    assert source.bars_feed == "sip"
    source.collect(["HOT"], AS_OF, CONFIG)
    snapshot_calls = [p for path, p in source.calls if path == "/v2/stocks/snapshots"]
    bar_calls = [p for path, p in source.calls if path == "/v2/stocks/bars"]
    assert {call["feed"] for call in snapshot_calls} == {"delayed_sip"}
    assert {call["feed"] for call in bar_calls} == {"sip"}


def test_the_iex_feed_carries_its_own_volume_warning() -> None:
    source = build_source(feed="iex")
    assert source.bars_feed == "iex"
    _, warnings, _, _, _ = source.collect(["HOT"], AS_OF, CONFIG)
    assert any("few percent of consolidated volume" in warning for warning in warnings)


def test_every_supported_feed_maps_to_a_bars_feed() -> None:
    assert set(_BARS_FEED) == {"delayed_sip", "sip", "iex"}


# ------------------------------------------------------------- measurement


def test_the_collector_measures_one_symbol_end_to_end() -> None:
    source = build_source(
        float_provider=StubFloats(
            {"HOT": FloatFact("HOT", 4_000_000, "test", SESSION.isoformat())}
        )
    )
    snapshots, _, session_date, phase, examined = source.collect(["HOT"], AS_OF, CONFIG)

    assert session_date == SESSION
    assert phase == "premarket"  # 09:30 wall clock, 09:15 after the feed delay
    assert examined == 1
    measured = snapshots[0]

    # Price is the last bar close inside the window, not the snapshot's last
    # trade: every figure on the row has to describe the same moment.
    assert measured.price == 3.05
    assert measured.premarket_volume == 1_200_000
    assert measured.session_volume == 1_200_000
    # 04:00–09:15 today over the same window on the prior sessions.
    assert measured.rvol == pytest.approx(1_200_000 / 20_000)
    # Excludes the 09:00 bar the price came from.
    assert measured.premarket_high == 2.85
    # Volume-weighted typical price ((h+l+c)/3) over the three in-window bars.
    assert measured.vwap == pytest.approx(2.8028, abs=1e-3)
    assert measured.vwap_anchor.startswith("premarket")
    assert measured.previous_close == 2.00
    assert measured.previous_high == 2.40
    assert measured.gap_percent == pytest.approx(52.5)
    assert measured.float_shares == 4_000_000
    assert measured.headlines[0].startswith("HOT wins FDA")
    assert measured.data_as_of == datetime.combine(SESSION, time(9, 15), tzinfo=EASTERN).astimezone(
        UTC
    )


def test_volume_printed_after_the_cutoff_never_enters_a_measurement() -> None:
    """The stub's 10:00 bar is nine million shares. If the window leaked, every
    volume, VWAP and RVOL on the row would be wrong by orders of magnitude."""
    source = build_source()
    snapshots, _, _, _, _ = source.collect(["HOT"], AS_OF, CONFIG)
    assert snapshots[0].session_volume == 1_200_000


def test_todays_partial_daily_bar_never_becomes_yesterdays_close() -> None:
    """The daily-bar response includes today. Reading its close as the previous
    close would compute the gap against the price it is measuring."""
    source = build_source()
    snapshots, _, _, _, _ = source.collect(["HOT"], AS_OF, CONFIG)
    assert snapshots[0].previous_close == 2.00
    assert snapshots[0].sma == pytest.approx(2.00)


def test_the_screener_accepts_what_the_collector_produces() -> None:
    source = build_source(
        float_provider=StubFloats(
            {"HOT": FloatFact("HOT", 4_000_000, "test", SESSION.isoformat())}
        )
    )
    snapshots, warnings, session_date, phase, _ = source.collect(["HOT"], AS_OF, CONFIG)
    result = BlowingStocksScreener(CONFIG).run(
        snapshots,
        provider=source.name,
        generated_at="now",
        session_date=session_date,
        session_phase=phase,
        warnings=warnings,
    )
    assert [item.symbol for item in result.low_float] == ["HOT"]
    assert result.low_float[0].keywords == ["FDA", "AI", "Partnership"]


# --------------------------------------------------------------- prefilter


def test_the_prefilter_keeps_movers_and_drops_the_rest() -> None:
    source = build_source()
    snapshots, _, _, _, examined = source.collect(["HOT", "QUIET", "PRICEY"], AS_OF, CONFIG)
    assert examined == 1
    assert [item.symbol for item in snapshots] == ["HOT"]


def test_a_volume_surge_passes_the_prefilter_without_any_gap() -> None:
    """Screen B gates on a surge and asks for no gap at all, so a prefilter
    that demanded one would make that screen unreachable."""
    source = build_source()
    source.responses["/v2/stocks/snapshots"]["SURGE"] = {
        "dailyBar": _daily(SESSION, 4.00, 4.05, 900_000),
        "prevDailyBar": _daily(SESSION - timedelta(days=1), 4.00, 4.10, 100_000),
        "latestTrade": {"p": 4.00, "t": _stamp(time(9, 17))},
        "minuteBar": _minute(time(9, 15), 4.00, 4.02, 1_000),
    }
    survivors = source._prefilter(source.responses["/v2/stocks/snapshots"], SESSION, CONFIG)
    assert "SURGE" in [symbol for symbol, _ in survivors]


def test_the_prefilter_cap_keeps_the_hottest_names() -> None:
    """When more names survive than can be measured, the ones dropped must be
    the least active — not the ones whose ticker sorts last."""
    source = build_source()
    snapshots = dict(source.responses["/v2/stocks/snapshots"])
    for index, symbol in enumerate(("AAA", "ZZZ")):
        snapshots[symbol] = {
            "dailyBar": _daily(SESSION, 2.0 + index, 2.1 + index, 900_000),
            "prevDailyBar": _daily(SESSION - timedelta(days=1), 1.0, 1.1, 10_000),
            "latestTrade": {"p": 2.0 + index * 5, "t": _stamp(time(9, 17))},
            "minuteBar": _minute(time(9, 15), 2.0, 2.1, 1_000),
        }
    capped = source._prefilter(snapshots, SESSION, BlowingStocksConfig(max_deep_scan_symbols=1))
    assert len(capped) == 1
    assert capped[0][0] == "ZZZ"  # the larger gap, not the alphabetical last


def _snaps(dates: dict[date, int]) -> dict[str, dict]:
    """A snapshot batch where ``count`` symbols carry a daily bar for ``day``."""
    batch: dict[str, dict] = {}
    index = 0
    for day, count in dates.items():
        for _ in range(count):
            batch[f"S{index}"] = {"dailyBar": _daily(day, 1.0, 1.1, 1_000)}
            index += 1
    return batch


def test_a_few_symbols_printing_premarket_prove_the_session_is_live() -> None:
    """The bug this exists to prevent: before the bell only the handful of
    symbols actually trading have today's daily bar, and the six thousand that
    do not still carry yesterday's. Taking the majority view concluded the
    market was closed every single morning — while the screener's targets were
    mid-explosion."""
    source = build_source()
    yesterday = SESSION - timedelta(days=1)
    premarket = datetime.combine(SESSION, time(9, 15), tzinfo=EASTERN)

    batch = _snaps({yesterday: 6_000, SESSION: 4})
    assert source._resolve_session(batch, premarket) == (SESSION, "premarket")


def test_a_quiet_premarket_is_still_todays_session_not_a_closed_market() -> None:
    """Before the bell, no prints proves nothing — a quiet premarket looks
    exactly like a holiday from here. The prefilter then reports finding
    nothing, which is true, instead of the report claiming the market never
    opened, which might not be."""
    source = build_source()
    batch = _snaps({SESSION - timedelta(days=1): 6_000})
    cutoff = datetime.combine(SESSION, time(9, 15), tzinfo=EASTERN)
    assert source._resolve_session(batch, cutoff) == (SESSION, "premarket")


def test_after_the_bell_an_empty_tape_really_does_mean_closed() -> None:
    """Once the session has begun, absence is conclusive: if the market were
    open, thousands of symbols would have traded."""
    source = build_source()
    yesterday = SESSION - timedelta(days=1)
    batch = _snaps({yesterday: 6_000})
    midday = datetime.combine(SESSION, time(12, 0), tzinfo=EASTERN)
    assert source._resolve_session(batch, midday) == (yesterday, "closed")


def test_a_weekend_run_is_closed_whatever_the_hour() -> None:
    source = build_source()
    saturday = datetime(2026, 8, 22, 9, 15, tzinfo=EASTERN)
    batch = _snaps({SESSION: 6_000})
    assert source._resolve_session(batch, saturday) == (SESSION, "closed")


def test_one_mis_stamped_row_cannot_speak_for_the_whole_market() -> None:
    source = build_source()
    yesterday = SESSION - timedelta(days=1)
    batch = _snaps({yesterday: 6_000, SESSION: 1})
    midday = datetime.combine(SESSION, time(12, 0), tzinfo=EASTERN)
    assert source._resolve_session(batch, midday) == (SESSION, "closed")


def test_the_session_is_live_once_the_tape_confirms_it_intraday() -> None:
    source = build_source()
    batch = _snaps({SESSION: 6_000})
    midday = datetime.combine(SESSION, time(12, 0), tzinfo=EASTERN)
    assert source._resolve_session(batch, midday) == (SESSION, "regular")


def test_an_empty_snapshot_batch_dates_no_session() -> None:
    source = build_source()
    assert source._resolve_session({}, AS_OF) == (None, "closed")


# ------------------------------------------------------------ closed market


def test_a_closed_market_is_reported_and_nothing_expensive_is_fetched() -> None:
    """The screener runs every day, including days the market does not. Gap,
    RVOL and the breakout gates all describe a live session, so there is
    nothing to screen — and no reason to spend the requests finding that out."""
    source = build_source()
    saturday = datetime(2026, 8, 22, 9, 30, tzinfo=EASTERN)
    snapshots, warnings, session_date, phase, examined = source.collect(
        ["HOT"], saturday, CONFIG
    )
    assert snapshots == [] and examined == 0
    assert phase == "closed"
    assert session_date == SESSION
    assert any("not open for business" in warning for warning in warnings)
    assert [path for path, _ in source.calls] == ["/v2/stocks/snapshots"]


def test_a_universe_that_is_entirely_quiet_says_so_rather_than_failing() -> None:
    source = build_source()
    snapshots, warnings, _, _, examined = source.collect(["QUIET"], AS_OF, CONFIG)
    assert snapshots == [] and examined == 0
    assert any("prefilter passed nothing" in warning.lower() for warning in warnings)


def test_a_feed_with_no_snapshots_at_all_is_an_error_not_an_empty_report() -> None:
    source = StubSource({"/v2/stocks/snapshots": {}})
    with pytest.raises(ProviderError, match="no snapshots"):
        source.collect(["HOT"], AS_OF, CONFIG)


# ------------------------------------------------------------------- news


def test_news_is_paged_so_no_symbol_looks_newsless_by_accident() -> None:
    """One page of fifty items across a hundred symbols leaves most of them
    looking newsless, and "has news" is a gate — a missed page silently
    rejects names that had a catalyst all along."""
    source = build_source()
    pages = [
        {
            "news": [
                {"headline": "page one", "symbols": ["HOT"], "created_at": _stamp(time(6, 0))}
            ],
            "next_page_token": "next",
        },
        {
            "news": [
                {"headline": "page two", "symbols": ["HOT"], "created_at": _stamp(time(7, 0))}
            ],
            "next_page_token": None,
        },
    ]
    source.responses["/v1beta1/news"] = lambda params: pages[1 if params.get("page_token") else 0]
    collected, available = source._news(["HOT"], SESSION)
    assert available is True
    assert [item["headline"] for item in collected["HOT"]] == ["page one", "page two"]


def test_a_failed_news_request_leaves_the_gate_unproven_not_passed() -> None:
    source = build_source()
    source.responses["/v1beta1/news"] = ProviderError("news is down")
    snapshots, warnings, _, _, _ = source.collect(["HOT"], AS_OF, CONFIG)
    assert snapshots[0].news_available is False
    assert snapshots[0].has_news is None
    assert any("did not answer" in warning for warning in warnings)
    outcome = BlowingStocksScreener(CONFIG).screen_low_float(snapshots[0])
    assert "news_unproven" in outcome


def test_the_api_error_message_is_carried_into_the_failure() -> None:
    """A bare "HTTP 400" costs an hour; "invalid feed: delayed_sip" costs none."""
    import urllib.error

    def explode(url, timeout=None):
        raise urllib.error.HTTPError(
            url, 400, "Bad Request", {}, _Body(json.dumps({"message": "invalid feed: nope"}))
        )

    original = blowing_sources.urllib.request.urlopen
    blowing_sources.urllib.request.urlopen = explode
    try:
        with pytest.raises(ProviderError, match="invalid feed: nope"):
            blowing_sources._get_json("https://example.invalid", "bars", {}, 5)
    finally:
        blowing_sources.urllib.request.urlopen = original


class _Body:
    def __init__(self, text: str) -> None:
        self._text = text.encode()

    def read(self) -> bytes:
        return self._text

    def close(self) -> None:
        return None


# --------------------------------------------------------------- discovery


NASDAQ_HEADER = (
    "Symbol|Security Name|Market Category|Test Issue|"
    "Financial Status|Round Lot Size|ETF|NextShares"
)
NASDAQ_FILE = NASDAQ_HEADER + """
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
SPY|An ETF|Q|N|N|100|Y|N
ZTEST|A test issue|Q|Y|N|100|N|N
ABCDW|Some Warrants|Q|N|N|100|N|N
LONGSYMBOL|Too long|Q|N|N|100|N|N
File Creation Time: 0819202621:31|||||||
"""

OTHER_HEADER = (
    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol"
)
OTHER_FILE = OTHER_HEADER + """
A|Agilent Technologies|N|A|N|100|N|A
IVV|An ETF|P|IVV|Y|100|N|IVV
BRK.A|Class A shares|N|BRK.A|N|100|N|BRK.A
File Creation Time: 0819202621:31||||||
"""


def test_discovery_keeps_common_stock_and_drops_everything_else(monkeypatch) -> None:
    """ETFs and test issues carry their own flags; warrants, rights and units
    are different instruments with their own float behaviour."""

    class Response:
        def __init__(self, text: str) -> None:
            self.text = text

        def read(self):
            return self.text.encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        return Response(NASDAQ_FILE if "nasdaqlisted" in url else OTHER_FILE)

    monkeypatch.setattr(blowing_sources.urllib.request, "urlopen", fake_open)
    symbols, warnings = discover_universe()
    assert symbols == ["AAPL", "A"]
    assert warnings == []


def test_discovery_that_returns_nothing_fails_loudly(monkeypatch) -> None:
    def fake_open(request, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(blowing_sources.urllib.request, "urlopen", fake_open)
    with pytest.raises(ProviderError, match="universe_file"):
        discover_universe()


# -------------------------------------------------------------------- demo


def test_the_demo_source_is_offline_deterministic_and_labelled() -> None:
    first, warnings, session_date, phase, examined = DemoBlowingSource().collect(
        ["AAA", "BBB"], datetime.now(UTC), CONFIG
    )
    second, _, _, _, _ = DemoBlowingSource().collect(["AAA", "BBB"], datetime.now(UTC), CONFIG)
    assert [item.symbol for item in first] == ["AAA", "BBB"] and examined == 2
    assert [item.price for item in first] == [item.price for item in second]
    assert phase == "premarket" and session_date is not None
    assert any("DEMO DATA" in warning for warning in warnings)
    assert all("DEMO" in (item.float_basis or "") for item in first)


# ------------------------------------------------------------ float fetch


class StubEdgar(FloatProvider):
    def __init__(self, concepts: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self.concepts = concepts
        self.requested: list[str] = []

    def ticker_map(self) -> dict[str, int]:
        return {"HOT": 1, "COLD": 2}

    def _get_json(self, url: str, label: str) -> dict:
        self.requested.append(label)
        payload = self.concepts.get(label)
        if payload is None:
            raise ProviderError(f"SEC has no filings at {label}")
        return payload


def _concept(unit: str, rows: list[tuple[str, float]]) -> dict:
    return {"units": {unit: [{"end": end, "val": value} for end, value in rows]}}


def test_filed_facts_become_a_float_and_are_cached(tmp_path: Path) -> None:
    cache = tmp_path / "float-cache.json"
    concepts = {
        "dei/EntityPublicFloat": _concept(
            "USD", [("2025-06-30", 1_000_000), ("2026-06-30", 8_000_000)]
        ),
        "dei/EntityCommonStockSharesOutstanding": _concept("shares", [("2026-07-31", 9_000_000)]),
    }
    provider = StubEdgar(concepts, cache_path=cache, today=SESSION)
    facts, warnings = provider.fetch(["HOT"], lambda symbol, on: (4.0, date(2026, 6, 30)))

    assert facts["HOT"].shares == 2_000_000  # newest public float, priced
    assert facts["HOT"].is_upper_bound is False
    assert warnings == []
    assert cache.exists()

    # A second provider reads the cache instead of asking EDGAR again.
    warm = StubEdgar(concepts, cache_path=cache, today=SESSION)
    again, _ = warm.fetch(["HOT"], lambda symbol, on: (4.0, date(2026, 6, 30)))
    assert again["HOT"].shares == 2_000_000
    assert warm.requested == []


def test_an_expired_cache_entry_is_refetched(tmp_path: Path) -> None:
    cache = tmp_path / "float-cache.json"
    cache.write_text(
        json.dumps(
            {"symbols": {"HOT": {"cached_at": 0, "facts": {"shares_outstanding": 1}}}}
        ),
        encoding="utf-8",
    )
    concepts = {
        "dei/EntityCommonStockSharesOutstanding": _concept("shares", [("2026-07-31", 9_000_000)])
    }
    provider = StubEdgar(concepts, cache_path=cache, cache_days=7, today=SESSION)
    facts, _ = provider.fetch(["HOT"])
    assert facts["HOT"].shares == 9_000_000
    assert provider.requested


def test_a_symbol_with_no_cik_is_unknown_rather_than_zero() -> None:
    provider = StubEdgar({}, today=SESSION)
    facts, warnings = provider.fetch(["NOTLISTED"])
    assert facts["NOTLISTED"].shares is None
    assert "no SEC CIK" in facts["NOTLISTED"].note
    assert any("no SEC CIK" in warning for warning in warnings)


def test_an_override_wins_over_the_filings_and_skips_the_fetch(tmp_path: Path) -> None:
    overrides = tmp_path / "float_overrides.csv"
    overrides.write_text("symbol,float_shares,as_of,source\nHOT,777000,,broker\n", encoding="utf-8")
    provider = StubEdgar({}, overrides_path=overrides, today=SESSION)
    facts, _ = provider.fetch(["HOT"])
    assert facts["HOT"].shares == 777_000
    assert provider.requested == []


def test_an_unreadable_cache_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    cache = tmp_path / "float-cache.json"
    cache.write_text("not json at all", encoding="utf-8")
    provider = StubEdgar(
        {"dei/EntityCommonStockSharesOutstanding": _concept("shares", [("2026-07-31", 5_000_000)])},
        cache_path=cache,
        today=SESSION,
    )
    facts, _ = provider.fetch(["HOT"])
    assert facts["HOT"].shares == 5_000_000
