"""Fundamental metrics sourced from SEC EDGAR XBRL company facts.

Why EDGAR: it is the filed-with-the-regulator record, free, keyless, and
auditable back to a specific form and period end. Vendor fundamentals APIs
either need credentials or restate silently; a scanner whose whole design is
"an unmeasurable requirement is never a satisfied one" should read the filings.

Everything here follows the project rule that unknown stays unknown. A metric
the filings do not support is ``None`` and is recorded in
``Fundamentals.missing``; it is never zero-filled, interpolated, or treated as
a pass. Companies that do not file with the SEC (foreign issuers filing 20-F
with sparse tagging, most ADRs) will legitimately come back mostly empty.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from market_scanner.providers.alpaca import ProviderError

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

#: SEC's access policy requires a descriptive User-Agent that identifies the
#: caller. Operators should set SEC_CONTACT_EMAIL; the fallback stays generic
#: rather than embedding anyone's address.
_DEFAULT_CONTACT = "market-scanner-operator@example.com"

#: Quarterly durations run 13 weeks but filings vary by a few days; annual
#: periods vary more because of 52/53-week retail calendars.
_QUARTER_DAYS = (80, 100)
_ANNUAL_DAYS = (330, 400)

#: Revenue tagging is genuinely inconsistent across filers, so try the common
#: tags in order of specificity and take the first that yields usable periods.
_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "RevenuesNetOfInterestExpense",
)
_NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
_EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted")
_EQUITY_TAGS = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
_DEBT_TAGS = ("LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations")
_OPERATING_CASH_TAGS = ("NetCashProvidedByUsedInOperatingActivities",)

_ALL_METRICS = (
    "revenue_growth_pct",
    "revenue_ttm",
    "net_margin_pct",
    "eps_growth_pct",
    "eps_ttm",
    "debt_to_equity",
    "return_on_equity_pct",
    "operating_cash_flow_ttm",
)


@dataclass(frozen=True)
class Fundamentals:
    """Filed fundamentals for one symbol. ``None`` means the filings did not
    support the metric — never that the metric is zero or unfavourable."""

    symbol: str
    revenue_growth_pct: float | None = None
    revenue_ttm: float | None = None
    net_margin_pct: float | None = None
    eps_growth_pct: float | None = None
    eps_ttm: float | None = None
    debt_to_equity: float | None = None
    return_on_equity_pct: float | None = None
    operating_cash_flow_ttm: float | None = None
    fiscal_period_end: str | None = None
    source: str = "SEC EDGAR XBRL company facts"
    missing: tuple[str, ...] = ()
    note: str | None = None

    @property
    def profitable(self) -> bool | None:
        if self.net_margin_pct is None:
            return None
        return self.net_margin_pct > 0

    @property
    def coverage(self) -> float:
        """Share of metrics the filings actually supported, 0.0–1.0."""
        return round(1 - len(self.missing) / len(_ALL_METRICS), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revenue_growth_pct": self.revenue_growth_pct,
            "revenue_ttm": self.revenue_ttm,
            "net_margin_pct": self.net_margin_pct,
            "eps_growth_pct": self.eps_growth_pct,
            "eps_ttm": self.eps_ttm,
            "debt_to_equity": self.debt_to_equity,
            "return_on_equity_pct": self.return_on_equity_pct,
            "operating_cash_flow_ttm": self.operating_cash_flow_ttm,
            "profitable": self.profitable,
            "fiscal_period_end": self.fiscal_period_end,
            "coverage": self.coverage,
            "missing": list(self.missing),
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True)
class _Period:
    """One tagged XBRL fact reduced to what the calculations need."""

    start: date | None
    end: date
    value: float
    form: str
    days: int | None = field(default=None)

    @property
    def duration(self) -> int | None:
        if self.days is not None:
            return self.days
        if self.start is None:
            return None
        return (self.end - self.start).days


class FundamentalsProvider:
    """Fetches and derives fundamentals from EDGAR, one company facts document
    per symbol. Failures degrade to an empty ``Fundamentals`` for that symbol
    rather than failing the scan: a fundamentals outage must not silently
    delete every technically qualified name from the watchlist."""

    name = "sec-edgar"

    def __init__(self, *, timeout: int = 30, max_workers: int = 4) -> None:
        self.timeout = timeout
        # SEC asks for <=10 requests/second; a small pool stays well inside it.
        self.max_workers = max(1, min(max_workers, 8))
        contact = os.environ.get("SEC_CONTACT_EMAIL", _DEFAULT_CONTACT).strip()
        self.user_agent = f"california-market-scanner/0.1 ({contact or _DEFAULT_CONTACT})"
        self._ticker_map: dict[str, int] | None = None

    # ------------------------------------------------------------------ HTTP

    def _get_json(self, url: str, label: str) -> dict:
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ProviderError(f"SEC has no filings at {label}") from error
            hint = " (rate limited; slow down)" if error.code == 429 else ""
            raise ProviderError(f"SEC request failed HTTP {error.code} at {label}{hint}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"SEC request failed at {label}: {type(error).__name__}") from error

    def ticker_map(self) -> dict[str, int]:
        if self._ticker_map is None:
            payload = self._get_json(TICKER_MAP_URL, "company_tickers.json")
            self._ticker_map = {
                str(row["ticker"]).upper(): int(row["cik_str"])
                for row in payload.values()
                if row.get("ticker") and row.get("cik_str") is not None
            }
        return self._ticker_map

    # ------------------------------------------------------------- public API

    def fetch(self, symbols: Sequence[str]) -> tuple[dict[str, Fundamentals], list[str]]:
        """Return fundamentals per symbol plus human-readable warnings."""
        clean = [symbol.upper() for symbol in dict.fromkeys(symbols) if symbol]
        warnings: list[str] = []
        try:
            mapping = self.ticker_map()
        except ProviderError as error:
            return (
                {symbol: _unavailable(symbol, str(error)) for symbol in clean},
                [
                    "SEC ticker directory unavailable; every fundamental gate was NOT evaluated "
                    f"({error})."
                ],
            )

        unlisted = [symbol for symbol in clean if symbol not in mapping]
        results: dict[str, Fundamentals] = {
            symbol: _unavailable(symbol, "no SEC CIK for this ticker (foreign issuer, ETF, or ADR)")
            for symbol in unlisted
        }
        listed = [symbol for symbol in clean if symbol in mapping]
        if unlisted:
            warnings.append(
                f"No SEC CIK for {len(unlisted)} symbols; fundamental gates were NOT evaluated "
                f"for them: {', '.join(unlisted[:10])}" + ("…" if len(unlisted) > 10 else "")
            )

        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            fetched = pool.map(lambda s: self._one(s, mapping[s]), listed)
            for symbol, outcome in zip(listed, fetched, strict=True):
                results[symbol] = outcome
                if outcome.note and outcome.coverage == 0.0:
                    failures.append(symbol)
        if failures:
            warnings.append(
                f"SEC filings yielded no usable fundamentals for {len(failures)} symbols: "
                f"{', '.join(failures[:10])}" + ("…" if len(failures) > 10 else "")
            )
        return results, warnings

    def _one(self, symbol: str, cik: int) -> Fundamentals:
        try:
            payload = self._get_json(COMPANY_FACTS_URL.format(cik=cik), f"companyfacts/{symbol}")
        except ProviderError as error:
            return _unavailable(symbol, str(error))
        try:
            return derive_fundamentals(symbol, payload)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
            return _unavailable(symbol, f"unparsable company facts: {type(error).__name__}")


# --------------------------------------------------------------- derivations


def _unavailable(symbol: str, note: str) -> Fundamentals:
    return Fundamentals(symbol=symbol, missing=_ALL_METRICS, note=note)


def _facts(payload: dict, tag: str) -> tuple[_Period, ...]:
    """Flatten one us-gaap tag into periods, newest last, deduplicated.

    Later filings restate earlier ones; keeping the last occurrence of each
    (start, end) pair takes the most recently filed value.
    """
    entries = ((payload.get("facts") or {}).get("us-gaap") or {}).get(tag)
    if not entries:
        return ()
    unique: dict[tuple[date | None, date], _Period] = {}
    for unit, rows in (entries.get("units") or {}).items():
        if unit not in ("USD", "USD/shares"):
            continue
        for row in rows:
            end_raw, value = row.get("end"), row.get("val")
            if not end_raw or value is None:
                continue
            try:
                end = date.fromisoformat(end_raw)
                start = date.fromisoformat(row["start"]) if row.get("start") else None
            except (TypeError, ValueError):
                continue
            unique[(start, end)] = _Period(start, end, float(value), str(row.get("form") or ""))
    return tuple(sorted(unique.values(), key=lambda period: period.end))


#: A tag whose newest period trails the filer's newest period by more than this
#: has been abandoned mid-history and must not be read as current.
_STALE_TAG_DAYS = 200


def _select_tag(payload: dict, tags: Sequence[str], *, widest: bool = False) -> tuple[_Period, ...]:
    """Choose the tag that actually represents this filer's current line item.

    Preference order alone is not enough, because filers abandon tags midway:
    NVIDIA stopped tagging ``RevenueFromContractWithCustomer...`` in 2022 and
    moved to ``Revenues``, so taking the first populated tag silently reads a
    four-year-old series. Candidates are therefore restricted to tags that are
    still current relative to the filer's newest data, and only then resolved.

    ``widest`` breaks ties by the largest trailing value instead of preference
    order. That is correct where the competing tags are subsets of one line —
    SoFi tags both a $154M contract-revenue component and the $1.2B
    net-of-interest total — but wrong for EPS or net income, where the
    alternatives are genuinely different measures rather than partial ones.
    """
    populated = [(tag, _facts(payload, tag)) for tag in tags]
    populated = [(tag, periods) for tag, periods in populated if periods]
    if not populated:
        return ()
    newest = max(periods[-1].end for _, periods in populated)
    current = [
        (tag, periods)
        for tag, periods in populated
        if (newest - periods[-1].end).days <= _STALE_TAG_DAYS
    ]
    if not current:
        return ()
    if widest and len(current) > 1:
        def magnitude(item: tuple[str, tuple[_Period, ...]]) -> float:
            window = _trailing_twelve_months(item[1])
            return abs(window[0]) if window else 0.0

        return max(current, key=magnitude)[1]
    order = {tag: index for index, tag in enumerate(tags)}
    return min(current, key=lambda item: order[item[0]])[1]


def _in_range(period: _Period, bounds: tuple[int, int]) -> bool:
    duration = period.duration
    return duration is not None and bounds[0] <= duration <= bounds[1]


def _quarters(periods: Sequence[_Period]) -> tuple[_Period, ...]:
    return tuple(period for period in periods if _in_range(period, _QUARTER_DAYS))


def _instants(periods: Sequence[_Period]) -> tuple[_Period, ...]:
    """Balance-sheet facts carry no start date."""
    return tuple(period for period in periods if period.start is None)


def _yoy_growth(quarters: Sequence[_Period]) -> tuple[float | None, date | None]:
    """Growth of the newest quarter against the quarter ~1 year earlier.

    Returns ``None`` when no prior-year comparable exists, or when the base is
    non-positive — a percentage change off a zero or negative base is not a
    meaningful growth rate and must not be reported as one.
    """
    if not quarters:
        return None, None
    latest = quarters[-1]
    target = latest.end - timedelta(days=365)
    prior = min(
        (q for q in quarters[:-1] if abs((q.end - target).days) <= 20),
        key=lambda q: abs((q.end - target).days),
        default=None,
    )
    if prior is None or prior.value <= 0:
        return None, latest.end
    return round((latest.value / prior.value - 1) * 100, 2), latest.end


def _trailing_twelve_months(periods: Sequence[_Period]) -> tuple[float, date, date] | None:
    """TTM value with the window it covers, as ``(value, start, end)``.

    The window is returned so callers can prove two TTM figures describe the
    same twelve months before dividing one by the other. Falls back to a recent
    annual period when quarterly coverage is short, which is common for filers
    whose Q4 exists only inside the 10-K.
    """
    quarters = _quarters(periods)
    if len(quarters) >= 4:
        chosen: list[_Period] = []
        cursor: date | None = None
        for period in reversed(quarters):
            if cursor is None or period.end <= cursor:
                chosen.append(period)
                cursor = period.start - timedelta(days=1) if period.start else None
            if len(chosen) == 4:
                break
        first = chosen[-1] if chosen else None
        if len(chosen) == 4 and first is not None and (chosen[0].end - first.end).days <= 400:
            start = first.start or first.end
            return round(sum(period.value for period in chosen), 2), start, chosen[0].end
    annual = [period for period in periods if _in_range(period, _ANNUAL_DAYS)]
    if annual:
        latest = annual[-1]
        return round(latest.value, 2), latest.start or latest.end, latest.end
    return None


def _aligned(
    numerator: tuple[float, date, date] | None,
    denominator: tuple[float, date, date] | None,
) -> bool:
    """True when two TTM windows close on effectively the same date.

    Guards against dividing a current net income by a revenue series that
    stopped years earlier, which is what produced a 1,240% "net margin".
    """
    if numerator is None or denominator is None:
        return False
    return abs((numerator[2] - denominator[2]).days) <= 45


def derive_fundamentals(symbol: str, payload: dict) -> Fundamentals:
    """Compute the metric set from a raw EDGAR company facts document.

    Split out from the HTTP layer so it is directly testable against a saved
    filing document.
    """
    # Revenue and debt resolve by widest current tag because their alternatives
    # are partial components of one line; income and EPS resolve by preference.
    revenue = _select_tag(payload, _REVENUE_TAGS, widest=True)
    net_income = _select_tag(payload, _NET_INCOME_TAGS)
    eps = _select_tag(payload, _EPS_TAGS)
    equity = _instants(_select_tag(payload, _EQUITY_TAGS))
    debt = _instants(_select_tag(payload, _DEBT_TAGS, widest=True))
    operating_cash = _select_tag(payload, _OPERATING_CASH_TAGS)

    revenue_growth, period_end = _yoy_growth(_quarters(revenue))
    eps_growth, _ = _yoy_growth(_quarters(eps))
    eps_window = _trailing_twelve_months(eps)
    revenue_window = _trailing_twelve_months(revenue)
    income_window = _trailing_twelve_months(net_income)
    cash_window = _trailing_twelve_months(operating_cash)
    revenue_ttm = revenue_window[0] if revenue_window else None

    # Every ratio below requires its inputs to describe the same period.
    net_margin = (
        round(income_window[0] / revenue_window[0] * 100, 2)
        if _aligned(income_window, revenue_window) and revenue_window[0] > 0  # type: ignore[index]
        else None
    )
    latest_equity = equity[-1].value if equity else None
    equity_end = equity[-1].end if equity else None
    debt_to_equity = (
        round(debt[-1].value / latest_equity, 3)
        if debt
        and latest_equity is not None
        and latest_equity > 0
        and equity_end is not None
        and abs((debt[-1].end - equity_end).days) <= 45
        else None
    )
    return_on_equity = (
        round(income_window[0] / latest_equity * 100, 2)
        if income_window is not None
        and latest_equity is not None
        and latest_equity > 0
        and equity_end is not None
        and abs((income_window[2] - equity_end).days) <= 45
        else None
    )

    values = {
        "revenue_growth_pct": revenue_growth,
        "revenue_ttm": revenue_ttm,
        "net_margin_pct": net_margin,
        "eps_growth_pct": eps_growth,
        "eps_ttm": eps_window[0] if eps_window else None,
        "debt_to_equity": debt_to_equity,
        "return_on_equity_pct": return_on_equity,
        "operating_cash_flow_ttm": cash_window[0] if cash_window else None,
    }
    missing = tuple(name for name in _ALL_METRICS if values[name] is None)
    latest_end = period_end or (equity[-1].end if equity else None)
    return Fundamentals(
        symbol=symbol,
        **values,
        fiscal_period_end=latest_end.isoformat() if latest_end else None,
        missing=missing,
        note="no usable us-gaap facts in filings" if len(missing) == len(_ALL_METRICS) else None,
    )


def stale_after(fundamentals: Fundamentals, as_of: datetime, max_age_days: int) -> bool:
    """True when the newest filed period is older than the allowed age.

    Filings lag by design; this exists so a report can flag a company whose
    last usable period predates the freshness the operator asked for.
    """
    if not fundamentals.fiscal_period_end:
        return False
    try:
        end = date.fromisoformat(fundamentals.fiscal_period_end)
    except ValueError:
        return False
    return (as_of.date() - end).days > max_age_days
