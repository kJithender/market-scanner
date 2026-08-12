from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from market_scanner.models import ScanConfig
from market_scanner.providers.yahoo import (
    EASTERN,
    YahooProvider,
    _completed_sessions,
    _daily_series,
    _session_volumes,
)


def _weekdays(count: int, end: datetime) -> list[datetime]:
    days: list[datetime] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def _daily_payload(count: int, *, benchmark: bool = False) -> dict:
    """Build a Yahoo-shaped daily chart payload with a clean uptrend."""
    days = _weekdays(count, datetime(2026, 8, 10, 9, 30, tzinfo=EASTERN))
    stamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    price = 40.0
    for index, day in enumerate(days):
        growth = 1.004 if not benchmark else 1.002
        price *= growth
        stamps.append(int(day.timestamp()))
        opens.append(price * 0.995)
        highs.append(price * 1.02)
        lows.append(price * 0.98)
        closes.append(price)
        volumes.append(3_000_000 + index)
    return {
        "timestamp": stamps,
        "indicators": {
            "quote": [
                {
                    "open": opens,
                    "high": highs,
                    "low": lows,
                    "close": closes,
                    "volume": volumes,
                }
            ],
            "adjclose": [{"adjclose": closes}],
        },
    }


def _intraday_payload() -> dict:
    stamps, closes, volumes = [], [], []
    for day in (10, 11):
        for minute in range(0, 240, 5):
            moment = datetime(2026, 8, day, 9, 30, tzinfo=EASTERN) + timedelta(minutes=minute)
            stamps.append(int(moment.timestamp()))
            closes.append(58.0)
            volumes.append(1_000)
    return {
        "timestamp": stamps,
        "indicators": {"quote": [{"close": closes, "volume": volumes}]},
    }


def test_daily_series_applies_split_adjustment_and_skips_gaps() -> None:
    stamp = int(datetime(2026, 8, 10, 9, 30, tzinfo=EASTERN).timestamp())
    payload = {
        "timestamp": [stamp, stamp + 86_400],
        "indicators": {
            "quote": [
                {
                    "open": [10.0, None],
                    "high": [11.0, 12.0],
                    "low": [9.0, 8.0],
                    "close": [10.0, 11.0],
                    "volume": [1_000, 2_000],
                }
            ],
            # Half the raw close: a 2:1 split should halve every price field.
            "adjclose": [{"adjclose": [5.0, 5.5]}],
        },
    }
    bars = _daily_series(payload)
    assert len(bars) == 1, "row with a null open must be dropped, not guessed"
    assert bars[0].close == pytest.approx(5.0)
    assert bars[0].high == pytest.approx(5.5)
    assert bars[0].volume == 1_000


def test_completed_sessions_drops_todays_partial_bar() -> None:
    bars = _daily_series(_daily_payload(5))
    session_date = datetime(2026, 8, 10, tzinfo=EASTERN).date()
    kept = _completed_sessions(bars, session_date)
    assert all(bar.timestamp.astimezone(EASTERN).date() < session_date for bar in kept)
    assert len(kept) == len(bars) - 1


def test_premarket_run_reports_no_volume_evidence() -> None:
    as_of = datetime(2026, 8, 11, 6, 0, tzinfo=EASTERN)
    current, prior, method = _session_volumes((), as_of, True)
    assert current is None
    assert prior == ()
    assert "no extended-hours volume" in method


def test_intraday_run_measures_same_time_of_day_volume() -> None:
    series = tuple(
        (
            datetime.fromtimestamp(stamp, UTC).astimezone(EASTERN),
            close,
            volume,
        )
        for stamp, close, volume in zip(
            _intraday_payload()["timestamp"],
            _intraday_payload()["indicators"]["quote"][0]["close"],
            _intraday_payload()["indicators"]["quote"][0]["volume"],
            strict=True,
        )
    )
    as_of = datetime(2026, 8, 11, 11, 0, tzinfo=EASTERN)
    current, prior, method = _session_volumes(series, as_of, False)
    # 09:30 through 11:00 inclusive is 19 five-minute bars of 1,000 shares.
    assert current == 19_000
    assert prior == (19_000,)
    assert "regular-session volume through 11:00 ET" in method


def test_snapshot_marks_spread_and_reports_zeroed_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = YahooProvider()
    as_of = datetime(2026, 8, 11, 11, 0, tzinfo=EASTERN)

    def fake_chart(symbol: str, params: dict) -> dict:
        if params["interval"] == "1d":
            return _daily_payload(120, benchmark=symbol == "SPY")
        return _intraday_payload()

    monkeypatch.setattr(provider, "_chart", fake_chart)
    monkeypatch.setattr(provider, "_news", lambda _symbol, _as_of: ())

    snapshots, warnings = provider._get_snapshots_sync(["TEST"], as_of, ScanConfig())

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert "tight_spread" in snapshot.unverified_gates
    assert "rvol" not in snapshot.unverified_gates, "intraday volume is measurable"
    assert snapshot.quote.bid == 0.0 and snapshot.quote.ask == 0.0
    assert snapshot.current_premarket_volume == 19_000
    assert any("no bid/ask" in warning for warning in warnings)


def test_premarket_snapshot_marks_rvol_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = YahooProvider()
    as_of = datetime(2026, 8, 11, 6, 0, tzinfo=EASTERN)

    def fake_chart(symbol: str, params: dict) -> dict:
        if params["interval"] == "1d":
            return _daily_payload(120, benchmark=symbol == "SPY")
        return _intraday_payload()

    monkeypatch.setattr(provider, "_chart", fake_chart)
    monkeypatch.setattr(provider, "_news", lambda _symbol, _as_of: ())

    snapshots, warnings = provider._get_snapshots_sync(["TEST"], as_of, ScanConfig())

    assert {"tight_spread", "rvol"} <= set(snapshots[0].unverified_gates)
    assert any("RVOL was NOT evaluated" in warning for warning in warnings)


def test_news_filters_unrelated_and_stale_items(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = YahooProvider()
    as_of = datetime(2026, 8, 11, 11, 0, tzinfo=EASTERN)
    recent = int((as_of - timedelta(hours=2)).timestamp())
    stale = int((as_of - timedelta(days=9)).timestamp())
    monkeypatch.setattr(
        provider,
        "_get_json",
        lambda _url, _label: {
            "news": [
                {"title": "Relevant headline", "providerPublishTime": recent,
                 "relatedTickers": ["TEST"], "link": "https://example.invalid/a"},
                {"title": "Other company", "providerPublishTime": recent,
                 "relatedTickers": ["OTHER"]},
                {"title": "Old news", "providerPublishTime": stale, "relatedTickers": ["TEST"]},
            ]
        },
    )
    catalysts = provider._news("TEST", as_of)
    assert [catalyst.description for catalyst in catalysts] == ["Relevant headline"]
