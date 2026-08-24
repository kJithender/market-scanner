"""Yahoo Finance adapter using only Python's standard library.

Yahoo's public chart endpoint serves prices, daily OHLCV, and extended-hours
prices without credentials. Two things it does **not** serve matter here:

* **No bid/ask.** The quote endpoints require a session crumb and answer 401,
  so the tight-spread gate cannot be measured.
* **No extended-hours volume.** Pre/post-market bars carry prices but report
  zero volume at every interval, so premarket RVOL cannot be measured.

Both gates are reported through ``MarketSnapshot.unverified_gates`` so the
scanner leaves them unevaluated. Nothing here invents a spread or a volume.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from market_scanner.models import Bar, Catalyst, MarketSnapshot, Quote, ScanConfig

# Shared with the Alpaca adapter so the CLI keeps one provider failure type.
from market_scanner.providers.alpaca import ProviderError

EASTERN = ZoneInfo("America/New_York")
BENCHMARK = "SPY"
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
USER_AGENT = "california-market-scanner/0.1 (+https://github.com/adhabnr-ux/california-market-scanner)"

NO_SPREAD_WARNING = (
    "Yahoo Finance publishes no bid/ask quote; the tight-spread gate was NOT evaluated "
    "and remains unproven for every symbol listed."
)
NO_PREMARKET_VOLUME_WARNING = (
    "Yahoo Finance reports zero extended-hours volume, so premarket RVOL was NOT evaluated. "
    "This is the scanner's primary premarket filter; it is unproven in this run."
)
UNOFFICIAL_WARNING = (
    "Yahoo Finance data is unofficial, may be delayed or adjusted without notice, and is "
    "rate-limited. Verify every price, level, and catalyst in a broker feed before trading."
)


class YahooProvider:
    name = "yahoo"

    def __init__(self, *, timeout: int = 30, max_workers: int = 4) -> None:
        self.timeout = timeout
        self.max_workers = max(1, max_workers)
        self.base_url = "https://query1.finance.yahoo.com"

    async def get_snapshots(
        self, symbols: list[str], as_of: datetime, config: ScanConfig
    ) -> tuple[list[MarketSnapshot], list[str]]:
        return await asyncio.to_thread(self._get_snapshots_sync, symbols, as_of, config)

    def _get_snapshots_sync(
        self, symbols: list[str], as_of: datetime, config: ScanConfig
    ) -> tuple[list[MarketSnapshot], list[str]]:
        clean_symbols = [symbol.upper() for symbol in dict.fromkeys(symbols) if symbol]
        session_date = as_of.astimezone(EASTERN).date()
        warnings = [NO_SPREAD_WARNING, UNOFFICIAL_WARNING]

        daily, daily_failures = self._gather(
            [*clean_symbols, BENCHMARK], lambda symbol: self._daily_bars(symbol, config)
        )
        benchmark = _completed_sessions(daily.get(BENCHMARK, ()), session_date)
        if len(benchmark) < 61:
            raise ProviderError(
                "Yahoo returned insufficient SPY history; beta cannot be computed."
            )
        intraday, intraday_failures = self._gather(clean_symbols, self._intraday_series)
        news, _ = self._gather(clean_symbols, lambda symbol: self._news(symbol, as_of))

        premarket_run = as_of.astimezone(EASTERN).time() < REGULAR_OPEN
        if premarket_run:
            warnings.append(NO_PREMARKET_VOLUME_WARNING)

        snapshots: list[MarketSnapshot] = []
        for symbol in clean_symbols:
            bars = _completed_sessions(daily.get(symbol, ()), session_date)
            series = intraday.get(symbol, ())
            if not bars or not series:
                continue
            price, observed_at = series[-1][1], series[-1][0]
            if price <= 0:
                continue
            current_volume, prior_volumes, method = _session_volumes(series, as_of, premarket_run)
            unverified = ["tight_spread"]
            if current_volume is None or not prior_volumes:
                unverified.append("rvol")
                current_volume = current_volume or 0
                prior_volumes = ()
            snapshots.append(
                MarketSnapshot(
                    symbol=symbol,
                    price=price,
                    # Yahoo supplies no bid/ask; a zeroed quote fails closed if the
                    # spread gate is ever evaluated instead of silently scoring 0%.
                    quote=Quote(0.0, 0.0, observed_at),
                    daily_bars=bars[-config.history_days :],
                    benchmark_bars=benchmark[-config.history_days :],
                    current_premarket_volume=current_volume,
                    historical_premarket_volumes=prior_volumes,
                    catalysts=tuple(news.get(symbol, ())),
                    data_as_of=observed_at,
                    rvol_method=method,
                    unverified_gates=tuple(unverified),
                )
            )

        failed = sorted(set(daily_failures) | set(intraday_failures))
        if failed:
            warnings.append(
                f"Yahoo request failed for {len(failed)} symbols: {', '.join(failed[:10])}"
                + ("…" if len(failed) > 10 else "")
            )
        if len(snapshots) < len(clean_symbols):
            warnings.append(
                f"Yahoo returned complete price history for {len(snapshots)} of "
                f"{len(clean_symbols)} requested symbols."
            )
        stale = [
            snapshot.symbol
            for snapshot in snapshots
            if snapshot.data_as_of
            and as_of.astimezone(UTC) - snapshot.data_as_of.astimezone(UTC) > timedelta(minutes=30)
        ]
        if stale:
            warnings.append(
                f"Stale Yahoo timestamps (>30 minutes) for {len(stale)} symbols; "
                "verify market session/holiday status before acting."
            )
        return snapshots, warnings

    async def get_long_history(
        self, symbols: list[str], years: int
    ) -> tuple[dict[str, tuple[Bar, ...]], list[str]]:
        """Multi-year adjusted daily bars, for horizon work the scanner cannot do.

        The scan path truncates history to ``config.history_days`` and only
        requests two years when that exceeds 200, so it cannot measure a
        multi-year return. This is a separate, deliberately simple fetch.
        """
        return await asyncio.to_thread(self._long_history_sync, symbols, years)

    def _long_history_sync(
        self, symbols: list[str], years: int
    ) -> tuple[dict[str, tuple[Bar, ...]], list[str]]:
        clean = [symbol.upper() for symbol in dict.fromkeys(symbols) if symbol]
        history, failures = self._gather(
            clean,
            lambda symbol: _daily_series(
                self._chart(
                    symbol,
                    {
                        "interval": "1d",
                        "range": f"{max(1, years)}y",
                        "includePrePost": "false",
                        "events": "div,split",
                    },
                )
            ),
        )
        warnings = [UNOFFICIAL_WARNING]
        if failures:
            warnings.append(
                f"Yahoo request failed for {len(failures)} symbols: {', '.join(failures[:10])}"
                + ("…" if len(failures) > 10 else "")
            )
        return {symbol: bars for symbol, bars in history.items() if bars}, warnings

    def _gather(
        self, symbols: Sequence[str], worker: Callable[[str], Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Fetch per symbol concurrently; one bad symbol must not fail the run."""
        results: dict[str, Any] = {}
        failures: list[str] = []
        unique = list(dict.fromkeys(symbols))
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for symbol, outcome in zip(unique, pool.map(_safe(worker), unique), strict=True):
                if isinstance(outcome, ProviderError):
                    failures.append(symbol)
                else:
                    results[symbol] = outcome
        return results, failures

    def _daily_bars(self, symbol: str, config: ScanConfig) -> tuple[Bar, ...]:
        result = self._chart(
            symbol,
            {
                "interval": "1d",
                "range": "2y" if config.history_days > 200 else "1y",
                "includePrePost": "false",
                "events": "div,split",
            },
        )
        return _daily_series(result)

    def _intraday_series(self, symbol: str) -> tuple[tuple[datetime, float, int], ...]:
        result = self._chart(
            symbol,
            {"interval": "5m", "range": "1mo", "includePrePost": "true"},
        )
        stamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes, volumes = quote.get("close") or [], quote.get("volume") or []
        series: list[tuple[datetime, float, int]] = []
        for index, stamp in enumerate(stamps):
            close = _at(closes, index)
            if stamp is None or close is None:
                continue
            series.append(
                (
                    datetime.fromtimestamp(int(stamp), UTC).astimezone(EASTERN),
                    float(close),
                    int(_at(volumes, index) or 0),
                )
            )
        return tuple(series)

    def _news(self, symbol: str, as_of: datetime) -> tuple[Catalyst, ...]:
        payload = self._get_json(
            f"{self.base_url}/v1/finance/search?"
            + urllib.parse.urlencode(
                {"q": symbol, "newsCount": "8", "quotesCount": "0", "enableFuzzyQuery": "false"}
            ),
            f"news/{symbol}",
        )
        cutoff = as_of - timedelta(days=3)
        catalysts: list[Catalyst] = []
        for item in payload.get("news") or []:
            published = item.get("providerPublishTime")
            stamp = datetime.fromtimestamp(int(published), UTC) if published else None
            if stamp and stamp < cutoff:
                continue
            title = str(item.get("title") or "").strip()
            if not title or not _mentions(symbol, item, title):
                continue
            catalysts.append(Catalyst("news", title, stamp, item.get("link")))
            if len(catalysts) == 3:
                break
        return tuple(catalysts)

    def _chart(self, symbol: str, params: dict[str, str]) -> dict:
        url = (
            f"{self.base_url}/v8/finance/chart/{urllib.parse.quote(symbol)}?"
            + urllib.parse.urlencode(params)
        )
        payload = self._get_json(url, f"chart/{symbol}")
        chart = payload.get("chart") or {}
        if chart.get("error"):
            code = (chart["error"] or {}).get("code", "unknown")
            raise ProviderError(f"Yahoo chart error for {symbol}: {code}")
        results = chart.get("result") or []
        if not results:
            raise ProviderError(f"Yahoo returned no chart data for {symbol}")
        return results[0]

    def _get_json(self, url: str, label: str) -> dict:
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            hint = " (rate limited; retry later)" if error.code == 429 else ""
            raise ProviderError(
                f"Yahoo request failed with HTTP {error.code} at {label}{hint}"
            ) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(
                f"Yahoo request failed at {label}: {type(error).__name__}"
            ) from error


def _mentions(symbol: str, item: dict, title: str) -> bool:
    """True only when a headline is actually about this symbol.

    Yahoo's search endpoint returns market-wide wire copy alongside
    company news, and those items usually carry no ``relatedTickers``.
    Accepting them made "Exchange-Traded Funds Higher, Equity Futures Mixed"
    register as an NVDA catalyst, which is exactly the kind of unearned
    evidence the catalyst gate exists to exclude. Require the item to name the
    symbol in its related tickers or in the headline itself.
    """
    related = item.get("relatedTickers")
    if related:
        return symbol in related
    # No related tickers: fall back to a word-boundary match on the headline so
    # "S" does not match "Stocks" and "ON" does not match "Monday".
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])", title) is not None


def _safe(worker: Callable[[str], Any]) -> Callable[[str], Any]:
    def run(symbol: str) -> Any:
        try:
            return worker(symbol)
        except ProviderError as error:
            return error

    return run


def _at(values: Sequence[Any], index: int) -> Any:
    return values[index] if index < len(values) else None


def _daily_series(result: dict) -> tuple[Bar, ...]:
    """Build split/dividend-adjusted daily bars from a Yahoo chart result."""
    stamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0]
    adjusted = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    bars: list[Bar] = []
    for index, stamp in enumerate(stamps):
        open_, high, low, close = (
            _at(opens, index),
            _at(highs, index),
            _at(lows, index),
            _at(closes, index),
        )
        if stamp is None or None in (open_, high, low, close) or not close:
            continue
        adjusted_close = _at(adjusted, index)
        factor = float(adjusted_close) / float(close) if adjusted_close else 1.0
        bars.append(
            Bar(
                datetime.fromtimestamp(int(stamp), UTC).astimezone(EASTERN),
                float(open_) * factor,
                float(high) * factor,
                float(low) * factor,
                float(close) * factor,
                int(_at(volumes, index) or 0),
            )
        )
    return tuple(bars)


def _completed_sessions(bars: Sequence[Bar], session_date: date) -> tuple[Bar, ...]:
    """Drop today's partial bar so the previous close is a finished session."""
    return tuple(bar for bar in bars if bar.timestamp.astimezone(EASTERN).date() < session_date)


def _session_volumes(
    series: Sequence[tuple[datetime, float, int]],
    as_of: datetime,
    premarket_run: bool,
) -> tuple[int | None, tuple[int, ...], str]:
    """Cumulative same-time-of-day regular-session volume, when measurable.

    Yahoo zeroes extended-hours volume, so a premarket run has no volume
    evidence at all and reports ``None`` rather than a fabricated figure.
    """
    if premarket_run:
        return None, (), "unavailable: Yahoo publishes no extended-hours volume"
    now = as_of.astimezone(EASTERN)
    cutoff = min(now.time().replace(second=0, microsecond=0), REGULAR_CLOSE)
    totals: dict[date, int] = defaultdict(int)
    for stamp, _price, volume in series:
        if stamp.weekday() < 5 and REGULAR_OPEN <= stamp.time() <= cutoff:
            totals[stamp.date()] += volume
    current = totals.pop(now.date(), None)
    prior = tuple(value for _, value in sorted(totals.items())[-20:] if value > 0)
    return (
        current,
        prior,
        f"regular-session volume through {cutoff:%H:%M} ET / prior-session mean (Yahoo)",
    )
