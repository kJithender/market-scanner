"""Data acquisition for :mod:`market_scanner.blowing_stocks`.

The premarket scanner reads a curated 245-name universe, so it can afford to
pull deep history and 35 days of minute bars for every symbol. This screener
cannot: its universe is every listed US common stock, because the name that is
up 90% on an FDA headline this morning was not on anybody's list yesterday.

So the run is two stages.

**Stage 1 — discovery and prefilter.** The listed-security directory published
by Nasdaq Trader gives roughly seven thousand common stocks with ETFs, test
issues, warrants and units already flagged. One batched snapshot request per
500 of them returns last price, today's volume so far, and the previous
session's bar — enough to throw out everything outside the price band and
everything that is not actually moving, for about fifteen HTTP requests.

**Stage 2 — measurement.** Only the survivors get daily history, five-minute
bars, news, and SEC float. That is where the expensive per-symbol data lives,
and by then there are dozens of symbols rather than thousands.

The default feed is ``delayed_sip``: the full consolidated tape, fifteen
minutes late. That delay is the right trade here. ``iex`` is real-time but
carries only IEX's own executions — roughly three percent of the volume — so
a 500,000-share gate measured on it is not measuring what it claims to. Every
timestamp in the report is the data's, never the clock's.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from statistics import fmean
from zoneinfo import ZoneInfo

from market_scanner.blowing_stocks import BlowingSnapshot, BlowingStocksConfig
from market_scanner.float_data import FloatFact, FloatProvider
from market_scanner.models import Bar
from market_scanner.providers.alpaca import ProviderError, _parse_time

EASTERN = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EXTENDED_OPEN = time(4, 0)
USER_AGENT = "california-market-scanner/0.1 (blowing-stocks screener)"

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

#: Ordinary common-stock tickers. Five-letter Nasdaq symbols ending in W, R, U
#: are warrants, rights and units — different instruments with their own
#: float and price behaviour, and not what this screener is looking for.
_PLAIN_TICKER = re.compile(r"^[A-Z]{1,5}$")
_DERIVATIVE_SUFFIX = re.compile(r"^[A-Z]{4}[WRU]$")

#: Snapshot symbols per request. 500 keeps the URL near 2.6 kB, well inside
#: every limit in the path, and covers the whole market in ~15 requests.
_SNAPSHOT_CHUNK = 500

#: How many symbols must carry today's daily bar before the session counts as
#: live. Small on purpose: in premarket only the names actually trading have
#: one, and those are precisely this screener's targets. More than one guards
#: against a single stale or mis-stamped row speaking for the whole market.
_SESSION_CONFIRMING_SYMBOLS = 3

#: Consolidated-tape delay, in minutes. Applied to the measurement cutoff so
#: today's window and the prior sessions' windows close at the same clock time
#: — otherwise RVOL compares a partial window against full ones and reads low
#: for every symbol.
_FEED_DELAY_MINUTES = {"delayed_sip": 15, "sip": 0, "iex": 0}

#: The snapshot and bar endpoints do not accept the same feeds, and the naming
#: hides it. ``delayed_sip`` is a *snapshot* feed — the consolidated tape 15
#: minutes late — and the bars endpoint rejects it outright; there, the same
#: entitlement is spelled ``sip``, which serves anything older than 15 minutes
#: on the free plan. Requesting ``iex`` bars instead is not a smaller version
#: of the same thing: IEX prints so little premarket that five-minute bars come
#: back nearly empty, which would quietly zero out premarket volume, the
#: premarket high, VWAP, and RVOL — every measure screen A is built on.
_BARS_FEED = {"delayed_sip": "sip", "sip": "sip", "iex": "iex"}

IEX_VOLUME_WARNING = (
    "Feed is 'iex', which reports only IEX's own executions — a few percent of consolidated "
    "volume. Absolute volume gates and RVOL are measured against that partial tape and will "
    "understate real participation. Use 'delayed_sip' for tape-complete volume."
)


# ------------------------------------------------------------------ helpers


def _get_json(url: str, label: str, headers: dict[str, str], timeout: int) -> dict:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # The API states the reason in the body — "invalid feed", "subscription
        # does not permit", a bad date. Credentials live in the headers, never
        # the body, so quoting it costs nothing and saves an hour of guessing.
        try:
            detail = str((json.loads(error.read().decode("utf-8")) or {}).get("message") or "")
        except (ValueError, OSError):
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise ProviderError(f"request failed with HTTP {error.code} at {label}{suffix}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ProviderError(f"request failed at {label}: {type(error).__name__}") from error


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _bar(row: dict) -> Bar:
    return Bar(
        _parse_time(row["t"]),
        float(row["o"]),
        float(row["h"]),
        float(row["l"]),
        float(row["c"]),
        int(row.get("v") or 0),
    )


def _eastern_date(value: str) -> date:
    return _parse_time(value).astimezone(EASTERN).date()


def _floor_5min(moment: datetime) -> datetime:
    return moment.replace(minute=moment.minute - moment.minute % 5, second=0, microsecond=0)


def session_phase(cutoff: datetime, session_date: date) -> str:
    """Name the part of the session the measurement window closes in."""
    local = cutoff.astimezone(EASTERN)
    if local.date() != session_date:
        return "closed"
    if local.time() < EXTENDED_OPEN:
        return "closed"
    if local.time() < REGULAR_OPEN:
        return "premarket"
    if local.time() < REGULAR_CLOSE:
        return "regular"
    return "after-hours"


def discover_universe(*, timeout: int = 60) -> tuple[list[str], list[str]]:
    """Every listed US common stock, from the Nasdaq Trader symbol directory.

    Both files are published daily and free of credentials. ETFs and test
    issues carry their own flags and are dropped here; warrants, rights and
    units are dropped by ticker shape. What remains is the population this
    screener is actually looking through — a name is on it because it is
    listed, never because someone predicted it would move.
    """
    symbols: list[str] = []
    warnings: list[str] = []
    headers = {"User-Agent": USER_AGENT}

    def read(url: str) -> list[str]:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace").splitlines()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProviderError(
                f"symbol directory unavailable at {url}: {type(error).__name__}"
            ) from error

    try:
        for line in read(NASDAQ_LISTED_URL)[1:]:
            parts = line.split("|")
            # Symbol|Security Name|Market Category|Test Issue|Financial Status|Lot|ETF|NextShares
            if len(parts) < 8 or parts[3] != "N" or parts[6] != "N":
                continue
            symbols.append(parts[0])
    except ProviderError as error:
        warnings.append(f"Nasdaq-listed directory unavailable: {error}")
    try:
        for line in read(OTHER_LISTED_URL)[1:]:
            parts = line.split("|")
            # ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Lot|Test Issue|Nasdaq Symbol
            if len(parts) < 8 or parts[4] != "N" or parts[6] != "N":
                continue
            symbols.append(parts[0])
    except ProviderError as error:
        warnings.append(f"NYSE/NYSE American directory unavailable: {error}")

    clean = [
        symbol
        for symbol in dict.fromkeys(symbols)
        if _PLAIN_TICKER.match(symbol) and not _DERIVATIVE_SUFFIX.match(symbol)
    ]
    if not clean:
        raise ProviderError(
            "symbol discovery returned nothing; set universe_file to run from a fixed list"
        )
    return clean, warnings


# ------------------------------------------------------------------- source


class AlpacaBlowingSource:
    """Two-stage Alpaca collector feeding :class:`BlowingStocksScreener`."""

    name = "alpaca"

    def __init__(
        self,
        *,
        feed: str = "delayed_sip",
        timeout: int = 30,
        key_id: str | None = None,
        secret: str | None = None,
        float_provider: FloatProvider | None = None,
    ) -> None:
        import os

        self.feed = feed
        self.bars_feed = _BARS_FEED.get(feed, feed)
        self.timeout = timeout
        self.key_id = key_id if key_id is not None else os.environ.get("APCA_API_KEY_ID", "")
        self.secret = secret if secret is not None else os.environ.get("APCA_API_SECRET_KEY", "")
        if not self.key_id or not self.secret:
            raise ProviderError(
                "Alpaca credentials missing. Set APCA_API_KEY_ID and APCA_API_SECRET_KEY."
            )
        self.float_provider = float_provider
        self.base_url = "https://data.alpaca.markets"
        self._headers = {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    # ---------------------------------------------------------------- HTTP

    def _get(self, path: str, params: dict[str, str]) -> dict:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        return _get_json(url, path, self._headers, self.timeout)

    def _paged(
        self, path: str, params: dict[str, str], field: str, *, max_pages: int = 200
    ) -> dict[str, list[dict]]:
        combined: dict[str, list[dict]] = defaultdict(list)
        token: str | None = None
        for _ in range(max_pages):
            page = dict(params)
            if token:
                page["page_token"] = token
            payload = self._get(path, page)
            for symbol, rows in (payload.get(field) or {}).items():
                combined[symbol].extend(rows)
            token = payload.get("next_page_token")
            if not token:
                break
        return dict(combined)

    # ------------------------------------------------------------- stage 1

    def _snapshots(self, symbols: Sequence[str]) -> tuple[dict[str, dict], list[str]]:
        collected: dict[str, dict] = {}
        failures: list[str] = []
        for chunk in _chunks(list(symbols), _SNAPSHOT_CHUNK):
            try:
                payload = self._get(
                    "/v2/stocks/snapshots", {"symbols": ",".join(chunk), "feed": self.feed}
                )
            except ProviderError as error:
                failures.append(str(error))
                continue
            collected.update(
                {symbol: snap for symbol, snap in payload.items() if isinstance(snap, dict)}
            )
        return collected, failures

    @staticmethod
    def _resolve_session(
        snapshots: dict[str, dict], cutoff: datetime
    ) -> tuple[date | None, str]:
        """Which session is being screened, and what part of it we are in.

        The obvious implementation — take the daily-bar date most symbols
        agree on — is wrong here, and wrong in exactly the case this screener
        exists for. Before the opening bell only the handful of symbols
        actually trading premarket have a daily bar dated today; the six
        thousand that do not still carry yesterday's. The majority therefore
        outvotes the minority that matters, every morning, and the run
        concludes the market is closed while its targets are mid-explosion.

        So the clock proposes the session and the tape confirms it. A few
        symbols printing today is proof the session is live, whatever the
        majority says. Before the bell their absence proves nothing — a quiet
        premarket looks identical to a holiday from here — so the session
        still counts as today, and the prefilter reports finding nothing
        rather than the report claiming the market never opened. After the
        bell that absence is conclusive: if the market were open, thousands
        would have traded.
        """
        local = cutoff.astimezone(EASTERN)
        today = local.date()
        newest: date | None = None
        traded_today = 0
        for snap in snapshots.values():
            bar = snap.get("dailyBar") or {}
            if not bar.get("t"):
                continue
            stamp = _eastern_date(bar["t"])
            newest = stamp if newest is None else max(newest, stamp)
            if stamp == today:
                traded_today += 1
        if newest is None:
            return None, "closed"
        if traded_today >= _SESSION_CONFIRMING_SYMBOLS:
            return today, session_phase(cutoff, today)
        if local.weekday() < 5 and EXTENDED_OPEN <= local.time() < REGULAR_OPEN:
            return today, "premarket"
        return newest, "closed"

    def _prefilter(
        self,
        snapshots: dict[str, dict],
        session_date: date,
        config: BlowingStocksConfig,
    ) -> list[tuple[str, float]]:
        """Cheap first cut: in the price band and actually moving.

        Deliberately looser than either screen. The gap threshold here is half
        the screen's, and a volume surge against the previous session passes on
        its own, because screen B gates on a volume surge with no gap
        requirement at all. Anything this stage drops is never measured
        properly, so it drops only what neither screen could possibly want.
        """
        low, catalyst = config.low_float, config.catalyst
        min_price = min(low.min_price, catalyst.min_price)
        max_price = max(low.max_price, catalyst.max_price)
        gap_floor = low.min_gap_pct * config.prefilter_gap_fraction
        survivors: list[tuple[str, float]] = []
        for symbol, snap in snapshots.items():
            daily = snap.get("dailyBar") or {}
            previous = snap.get("prevDailyBar") or {}
            trade = snap.get("latestTrade") or {}
            minute = snap.get("minuteBar") or {}
            if not daily.get("t"):
                continue
            traded_today = _eastern_date(daily["t"]) == session_date
            previous_bar = daily if not traded_today else previous
            previous_close = float(previous_bar.get("c") or 0)
            price = float(trade.get("p") or minute.get("c") or daily.get("c") or 0)
            volume = int(daily.get("v") or 0) if traded_today else 0
            if price <= 0 or previous_close <= 0:
                continue
            if not min_price <= price <= max_price:
                continue
            if volume < config.prefilter_min_volume:
                continue
            gap = (price / previous_close - 1) * 100
            previous_volume = float(previous_bar.get("v") or 0)
            volume_ratio = volume / previous_volume if previous_volume > 0 else 0.0
            moving = gap >= gap_floor or volume_ratio >= config.prefilter_min_volume_ratio
            if not moving:
                continue
            # Heat orders the cap, so when more names survive than can be
            # measured, the ones dropped are the least active, not the ones
            # whose ticker sorts last.
            survivors.append((symbol, max(gap, 0.0) * max(volume_ratio, 0.1)))
        survivors.sort(key=lambda item: (-item[1], item[0]))
        return survivors[: config.max_deep_scan_symbols]

    # ------------------------------------------------------------- stage 2

    def _daily_bars(
        self, symbols: Sequence[str], session_date: date, config: BlowingStocksConfig
    ) -> dict[str, list[Bar]]:
        # Two years back so a public-float measurement date from the last 10-K
        # can be priced; split adjustment only, because dividend adjustment
        # would move highs and closes for a reason that has nothing to do with
        # share counts or breakouts.
        start = (session_date - timedelta(days=760)).isoformat()
        rows = self._paged(
            "/v2/stocks/bars",
            {
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": start,
                "adjustment": "split",
                "feed": self.bars_feed,
                "limit": "10000",
                "sort": "asc",
            },
            "bars",
        )
        return {symbol: [_bar(row) for row in values] for symbol, values in rows.items()}

    def _intraday_bars(
        self, symbols: Sequence[str], session_date: date, config: BlowingStocksConfig
    ) -> dict[str, list[Bar]]:
        # Enough calendar days to contain the RVOL lookback in sessions, with
        # slack for weekends and holidays.
        span = config.rvol_lookback_sessions * 2 + 7
        start = (session_date - timedelta(days=span)).isoformat()
        rows = self._paged(
            "/v2/stocks/bars",
            {
                "symbols": ",".join(symbols),
                "timeframe": "5Min",
                "start": start,
                "adjustment": "split",
                "feed": self.bars_feed,
                "limit": "10000",
                "sort": "asc",
            },
            "bars",
        )
        return {symbol: [_bar(row) for row in values] for symbol, values in rows.items()}

    def _news(
        self, symbols: Sequence[str], session_date: date
    ) -> tuple[dict[str, list[dict]], bool]:
        """Headlines for the last three days, paged until the window is covered.

        Paging matters. One page of fifty items across a hundred symbols leaves
        most of them looking newsless, and "has news" is a gate here — a missed
        page would silently reject names that had a catalyst all along.

        The second return value is whether the feed answered at all. A failed
        news request makes "has news" unmeasured for every symbol, which the
        screener treats as unproven rather than as an absence of news.
        """
        collected: dict[str, list[dict]] = defaultdict(list)
        wanted = set(symbols)
        token: str | None = None
        answered = False
        for _ in range(20):
            params = {
                "symbols": ",".join(symbols),
                "start": (session_date - timedelta(days=3)).isoformat(),
                "limit": "50",
                "sort": "desc",
            }
            if token:
                params["page_token"] = token
            try:
                payload = self._get("/v1beta1/news", params)
            except ProviderError:
                break
            answered = True
            for item in payload.get("news") or []:
                for symbol in item.get("symbols") or []:
                    if symbol in wanted and len(collected[symbol]) < 5:
                        collected[symbol].append(item)
            token = payload.get("next_page_token")
            if not token:
                break
        return dict(collected), answered

    # ------------------------------------------------------------ assembly

    def collect(
        self,
        symbols: Sequence[str],
        as_of: datetime,
        config: BlowingStocksConfig,
    ) -> tuple[list[BlowingSnapshot], list[str], date | None, str, int]:
        """Measure every symbol worth measuring.

        Returns the snapshots, warnings, the session actually being reported,
        the phase of that session, and how many symbols survived the prefilter.
        """
        warnings: list[str] = []
        if self.feed == "iex":
            warnings.append(IEX_VOLUME_WARNING)

        snapshots, failures = self._snapshots(symbols)
        warnings.extend(failures)
        if not snapshots:
            raise ProviderError("Alpaca returned no snapshots for the discovered universe")

        delay = _FEED_DELAY_MINUTES.get(self.feed, 15)
        cutoff = _floor_5min(as_of.astimezone(EASTERN) - timedelta(minutes=delay))
        session_date, phase = self._resolve_session(snapshots, cutoff)
        if session_date is None:
            raise ProviderError("no daily bars in the snapshot response; cannot date the session")
        if phase == "closed":
            warnings.append(
                f"US market not open for business at {cutoff:%Y-%m-%d %H:%M %Z}; the newest "
                f"session on the tape is {session_date}. Nothing was screened — the gap, RVOL "
                "and breakout gates all describe a live session."
            )
            return [], warnings, session_date, phase, 0

        survivors = self._prefilter(snapshots, session_date, config)
        chosen = [symbol for symbol, _ in survivors]
        if not chosen:
            warnings.append(
                "No symbol in the universe was inside the price band and moving; the prefilter "
                "passed nothing, so neither screen had anything to measure."
            )
            return [], warnings, session_date, phase, 0

        daily = self._daily_bars(chosen, session_date, config)
        intraday = self._intraday_bars(chosen, session_date, config)
        news, news_available = self._news(chosen, session_date)
        if not news_available:
            warnings.append(
                "The Alpaca news endpoint did not answer this run, so 'has news' was NOT "
                "measured. Screen A rejects every symbol on that unproven gate rather than "
                "treating silence as an absence of news."
            )
        floats = self._floats(chosen, daily, warnings)

        measured: list[BlowingSnapshot] = []
        for symbol in chosen:
            snapshot = _measure(
                symbol=symbol,
                snap=snapshots[symbol],
                daily_bars=daily.get(symbol, []),
                intraday_bars=intraday.get(symbol, []),
                news_items=news.get(symbol, []),
                float_fact=floats.get(symbol, FloatFact(symbol=symbol)),
                session_date=session_date,
                cutoff=cutoff,
                phase=phase,
                feed=self.feed,
                config=config,
                news_available=news_available,
            )
            if snapshot is not None:
                measured.append(snapshot)
        if len(measured) < len(chosen):
            warnings.append(
                f"{len(chosen) - len(measured)} of {len(chosen)} prefiltered symbols could not be "
                "measured (incomplete history or bars) and were dropped rather than guessed."
            )
        return measured, warnings, session_date, phase, len(chosen)

    def _floats(
        self, symbols: Sequence[str], daily: dict[str, list[Bar]], warnings: list[str]
    ) -> dict[str, FloatFact]:
        if self.float_provider is None:
            return {}

        def prices(symbol: str, on_or_before: date) -> tuple[float, date] | None:
            """Split-adjusted close on the last session at or before a date."""
            bars = daily.get(symbol) or []
            found: tuple[float, date] | None = None
            for bar in bars:
                stamp = bar.timestamp.astimezone(EASTERN).date()
                if stamp <= on_or_before:
                    found = (bar.close, stamp)
                else:
                    break
            return found

        facts, float_warnings = self.float_provider.fetch(symbols, prices)
        warnings.extend(float_warnings)
        return facts


def _measure(
    *,
    symbol: str,
    snap: dict,
    daily_bars: Sequence[Bar],
    intraday_bars: Sequence[Bar],
    news_items: Sequence[dict],
    float_fact: FloatFact,
    session_date: date,
    cutoff: datetime,
    phase: str,
    feed: str,
    config: BlowingStocksConfig,
    news_available: bool,
) -> BlowingSnapshot | None:
    """Turn raw feeds into one measured symbol, or ``None`` when it cannot be.

    Every measure here is derived from bars whose timestamps prove they belong
    to the window claimed. Nothing is inferred from the clock.
    """
    trade = snap.get("latestTrade") or {}
    minute = snap.get("minuteBar") or {}
    daily_row = snap.get("dailyBar") or {}

    today, prior_window, had_bars = _window_volumes(intraday_bars, session_date, cutoff)
    # Price comes from the same window as the volume, the VWAP and the
    # premarket high, so every figure on the row describes one moment. The
    # snapshot's last trade is whatever printed most recently — after hours,
    # that is a price from a session the rest of the row knows nothing about,
    # and a gap computed against it is fiction.
    price = today.close if today is not None else 0.0
    if price <= 0:
        stamp = trade.get("t") or minute.get("t")
        if stamp and _parse_time(stamp).astimezone(EASTERN).date() == session_date:
            price = float(trade.get("p") or minute.get("c") or 0)
    if price <= 0:
        price = float(daily_row.get("c") or 0)
    if price <= 0:
        return None

    completed = [
        bar for bar in daily_bars if bar.timestamp.astimezone(EASTERN).date() < session_date
    ]
    if not completed:
        return None
    previous = completed[-1]
    # A listing too young for the full window leaves the measure unmeasured
    # rather than approximating it from a shorter one, which would be a
    # different indicator wearing the same name.
    period = config.catalyst.sma_period
    sma = fmean(bar.close for bar in completed[-period:]) if len(completed) >= period else None
    average_days = config.catalyst.volume_average_days
    average_volume = (
        fmean(bar.volume for bar in completed[-average_days:])
        if len(completed) >= average_days
        else None
    )

    # A symbol the feed answered for, that simply did not trade in the window,
    # has a volume of zero — a measurement, not a gap in the data. One that the
    # feed returned nothing at all for is unmeasured. Collapsing the two would
    # either reject names that really were quiet or pass names nobody measured.
    session_volume = today.volume if today is not None else (0 if had_bars else None)
    premarket_volume = (
        today.premarket_volume if today is not None else (0 if had_bars else None)
    )
    premarket_high = today.premarket_high if today is not None else None
    high_basis = (
        today.premarket_high_basis if today is not None else "unmeasured: no trades in the window"
    )
    vwap, anchor = (
        (today.vwap, today.vwap_anchor)
        if today is not None
        else (None, "no trades in the measurement window" if had_bars else "unmeasured")
    )
    rvol: float | None = None
    method = "unmeasured: no prior same-window volume on this feed"
    if prior_window and session_volume is not None:
        baseline = fmean(prior_window)
        if baseline > 0:
            rvol = session_volume / baseline
            method = (
                f"cumulative volume 04:00–{cutoff:%H:%M} ET / mean of the same window over "
                f"the prior {len(prior_window)} sessions"
            )

    headlines = tuple(str(item.get("headline") or "").strip() for item in news_items)
    headlines = tuple(headline for headline in headlines if headline)
    details = tuple(
        {
            "headline": str(item.get("headline") or ""),
            "source": str(item.get("source") or ""),
            "created_at": str(item.get("created_at") or ""),
            "url": item.get("url"),
        }
        for item in news_items
    )
    return BlowingSnapshot(
        symbol=symbol,
        price=price,
        session_date=session_date,
        # The window's close, not the wall clock: every figure on this row was
        # measured through that moment and none of it is newer.
        data_as_of=cutoff.astimezone(UTC),
        previous_close=previous.close,
        previous_high=previous.high,
        sma=sma,
        sma_period=config.catalyst.sma_period,
        average_volume=average_volume,
        session_volume=session_volume,
        premarket_volume=premarket_volume,
        rvol=rvol,
        rvol_method=method,
        premarket_high=premarket_high,
        premarket_high_basis=high_basis,
        vwap=vwap,
        vwap_anchor=anchor,
        float_shares=float_fact.shares,
        float_basis=float_fact.basis,
        float_as_of=float_fact.as_of,
        float_is_upper_bound=float_fact.is_upper_bound,
        headlines=headlines,
        headline_details=details,
        news_available=news_available,
        feed=feed,
        session_phase=phase,
    )


class _TodayWindow:
    """Today's measurements inside the 04:00 → cutoff window."""

    __slots__ = (
        "volume",
        "premarket_volume",
        "premarket_high",
        "premarket_high_basis",
        "vwap",
        "vwap_anchor",
        "close",
    )

    def __init__(
        self,
        volume: int,
        premarket_volume: int,
        premarket_high: float | None,
        premarket_high_basis: str,
        vwap: float | None,
        vwap_anchor: str,
        close: float,
    ) -> None:
        self.volume = volume
        self.premarket_volume = premarket_volume
        self.premarket_high = premarket_high
        self.premarket_high_basis = premarket_high_basis
        self.vwap = vwap
        self.vwap_anchor = vwap_anchor
        self.close = close


def _window_volumes(
    bars: Sequence[Bar], session_date: date, cutoff: datetime
) -> tuple[_TodayWindow | None, list[int], bool]:
    """Today's window, the same window on prior sessions, and whether the feed
    returned anything at all for this symbol.

    Both windows close at the same clock time, which is the whole point: an
    RVOL that compares this morning's partial window against prior *full*
    sessions reads low for every symbol and would silently gut a gate set at 5.

    ``None`` for today's window means the symbol did not trade in it. The third
    value separates that from the feed having no data for the symbol — the
    caller reports the first as a volume of zero and the second as unmeasured.
    """
    local_cutoff = cutoff.astimezone(EASTERN).time()
    prior: dict[date, int] = defaultdict(int)
    volume = premarket_volume = 0
    premarket_highs: list[float] = []
    regular_notional = regular_volume = 0.0
    extended_notional = extended_volume = 0.0
    last_close = 0.0
    seen_today = False

    for bar in bars:
        stamp = bar.timestamp.astimezone(EASTERN)
        clock = stamp.time()
        if clock < EXTENDED_OPEN or clock >= local_cutoff or clock >= REGULAR_CLOSE:
            continue
        if stamp.date() == session_date:
            seen_today = True
            volume += bar.volume
            last_close = bar.close
            typical = (bar.high + bar.low + bar.close) / 3
            if clock < REGULAR_OPEN:
                premarket_volume += bar.volume
                premarket_highs.append(bar.high)
                extended_notional += typical * bar.volume
                extended_volume += bar.volume
            else:
                regular_notional += typical * bar.volume
                regular_volume += bar.volume
        elif stamp.date() < session_date and stamp.weekday() < 5:
            prior[stamp.date()] += bar.volume

    baseline = [value for _, value in sorted(prior.items()) if value > 0]
    if not seen_today:
        return None, baseline, bool(bars)

    # "Price > premarket high" means different things on either side of the
    # bell, and reading it the same way in both places makes it useless before
    # the open. Once the regular session has begun the premarket high is a
    # settled level and the price either cleared it or did not. Inside the
    # premarket it is a *running* high that already contains the bar the price
    # came from, so comparing the two can only ever be false — the level has to
    # exclude the current bar for the comparison to mean "making new highs
    # right now", which is what the condition is actually asking.
    if local_cutoff > REGULAR_OPEN:
        premarket_high = max(premarket_highs) if premarket_highs else None
        high_basis = "settled premarket high, 04:00–09:29 ET"
    elif len(premarket_highs) > 1:
        premarket_high = max(premarket_highs[:-1])
        high_basis = f"premarket high through the bar before {local_cutoff:%H:%M} ET"
    else:
        premarket_high = None
        high_basis = "unmeasured: too few premarket bars to have a prior high"
    # Anchored at the open once the regular session has traded, because that is
    # the VWAP every other participant is watching; before the bell the only
    # VWAP that exists is the premarket one, and the report names which.
    if regular_volume > 0:
        vwap: float | None = regular_notional / regular_volume
        anchor = "regular session, anchored 09:30 ET"
    elif extended_volume > 0:
        vwap = extended_notional / extended_volume
        anchor = "premarket, anchored 04:00 ET"
    else:
        vwap, anchor = None, "no volume in the measurement window"
    return (
        _TodayWindow(
            volume,
            premarket_volume,
            premarket_high,
            high_basis,
            vwap,
            anchor,
            last_close,
        ),
        baseline,
        True,
    )


# --------------------------------------------------------------------- demo


class DemoBlowingSource:
    """Deterministic offline source, for tests and setup validation.

    Fabricates nothing it does not label: every warning it returns says the
    data is synthetic, and the CLI refuses to treat a demo run as a real one.
    """

    name = "demo"

    def collect(
        self, symbols: Sequence[str], as_of: datetime, config: BlowingStocksConfig
    ) -> tuple[list[BlowingSnapshot], list[str], date, str, int]:
        session_date = as_of.astimezone(EASTERN).date()
        cutoff = as_of.astimezone(EASTERN).replace(hour=9, minute=20, second=0, microsecond=0)
        chosen = list(symbols)[:6] or ["DEMO"]
        snapshots: list[BlowingSnapshot] = []
        for index, symbol in enumerate(chosen):
            previous_close = 2.0 + index * 0.75
            price = previous_close * (1.45 - index * 0.09)
            premarket_volume = 900_000 - index * 120_000
            snapshots.append(
                BlowingSnapshot(
                    symbol=symbol,
                    price=round(price, 2),
                    session_date=session_date,
                    data_as_of=cutoff.astimezone(UTC),
                    previous_close=previous_close,
                    previous_high=previous_close * 1.04,
                    sma=previous_close * 0.85,
                    sma_period=config.catalyst.sma_period,
                    average_volume=180_000 + index * 20_000,
                    session_volume=premarket_volume,
                    premarket_volume=premarket_volume,
                    rvol=24.0 - index * 3.5,
                    rvol_method="DEMO DATA: synthetic same-window relative volume",
                    premarket_high=price * 0.985,
                    premarket_high_basis="DEMO DATA: synthetic prior premarket high",
                    vwap=price * 0.96,
                    vwap_anchor="premarket, anchored 04:00 ET (DEMO)",
                    float_shares=3_000_000 + index * 2_500_000,
                    float_basis="DEMO DATA: synthetic float",
                    float_as_of=session_date.isoformat(),
                    headlines=(
                        f"{symbol} announces FDA clearance and an AI partnership (DEMO DATA)",
                    ),
                    headline_details=(
                        {
                            "headline": f"{symbol} announces FDA clearance and an AI "
                            "partnership (DEMO DATA)",
                            "source": "demo",
                            "created_at": cutoff.isoformat(),
                            "url": None,
                        },
                    ),
                    feed="demo",
                    session_phase="premarket",
                )
            )
        return (
            snapshots,
            [
                "DEMO DATA: synthetic prices, volume, float, and headlines. Every row below is "
                "invented for pipeline validation. Do not trade it."
            ],
            session_date,
            "premarket",
            len(chosen),
        )


async def collect_async(source, symbols, as_of, config):
    """Run a synchronous source off the event loop, matching the scan path."""
    return await asyncio.to_thread(source.collect, symbols, as_of, config)
