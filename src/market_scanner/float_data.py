"""Share float reconstructed from SEC cover-page filings.

Float is the gate that defines the low-float screen, and no free market-data
feed publishes it. Rather than drop the gate or fabricate a number, it is
rebuilt from facts companies are *required* to state on the cover page of
every 10-K and 10-Q, in the ``dei`` XBRL namespace:

``EntityPublicFloat``
    The aggregate market value of common equity held by non-affiliates, in
    dollars, measured on the last business day of the most recent second
    fiscal quarter. This is genuinely *float* — insiders and affiliates are
    already excluded — but it is a dollar figure on a date some months back,
    so it has to be divided by the price on that date to become a share count.

``EntityCommonStockSharesOutstanding``
    Shares outstanding on the cover date. Not float: it includes insider and
    affiliate holdings, so it is an **upper bound**. A symbol that passes a
    float ceiling on this basis has genuinely passed it; one that fails may
    still have a small float the filings never broke out, which is why the
    report labels the basis on every row.

Three rules keep the result honest:

* An operator-supplied override always wins. A real float feed, or a figure
  read off a filing by hand, beats anything derived here.
* A share count is only ever expressed in *today's* share terms. The price
  used to convert ``EntityPublicFloat`` is the **split-adjusted** close on the
  measurement date, which folds every split since into the conversion —
  without it, one reverse split (routine in this universe) throws the answer
  off by exactly the split ratio.
* A fact that cannot be measured is ``None``. It is never zero-filled and
  never guessed from market cap.
"""

from __future__ import annotations

import csv
import json
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from market_scanner.fundamentals import FundamentalsProvider
from market_scanner.providers.alpaca import ProviderError

COMPANY_CONCEPT_URL = (
    "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/dei/{tag}.json"
)

#: Cover-page tags, in the order they are preferred as evidence of float.
PUBLIC_FLOAT_TAG = "EntityPublicFloat"
SHARES_OUTSTANDING_TAG = "EntityCommonStockSharesOutstanding"

#: A price on the float measurement date must come from within this many days
#: of it. Markets close for weekends and holidays, so the exact date is often
#: missing; a week of slack finds the right session without silently pricing
#: the float off a close from a different month.
_PRICE_WINDOW_DAYS = 7

#: When shares outstanding have grown by more than this multiple since the
#: float was measured, the company has diluted heavily and the older float
#: figure probably understates today's. The row keeps the figure and says so.
_DILUTION_FACTOR = 2.0

#: A close-price lookup: ``(symbol, on_or_before) -> split-adjusted close``.
PriceLookup = Callable[[str, date], tuple[float, date] | None]


@dataclass(frozen=True)
class FloatFact:
    """What the filings support about one symbol's float."""

    symbol: str
    shares: float | None = None
    #: How ``shares`` was arrived at, for display on every report row.
    basis: str | None = None
    #: The date the underlying fact was measured, ISO format.
    as_of: str | None = None
    #: True when ``shares`` is shares outstanding, which float cannot exceed.
    is_upper_bound: bool = False
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "float_shares": self.shares,
            "basis": self.basis,
            "as_of": self.as_of,
            "is_upper_bound": self.is_upper_bound,
            "note": self.note,
        }


@dataclass(frozen=True)
class _RawFacts:
    """The two cover-page facts, before any price is applied."""

    public_float_usd: float | None = None
    public_float_as_of: str | None = None
    shares_outstanding: float | None = None
    shares_outstanding_as_of: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "public_float_usd": self.public_float_usd,
            "public_float_as_of": self.public_float_as_of,
            "shares_outstanding": self.shares_outstanding,
            "shares_outstanding_as_of": self.shares_outstanding_as_of,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> _RawFacts:
        return cls(
            public_float_usd=payload.get("public_float_usd"),
            public_float_as_of=payload.get("public_float_as_of"),
            shares_outstanding=payload.get("shares_outstanding"),
            shares_outstanding_as_of=payload.get("shares_outstanding_as_of"),
            note=payload.get("note"),
        )


def load_overrides(path: str | Path | None) -> dict[str, FloatFact]:
    """Read the operator's float override CSV, if one exists.

    Columns: ``symbol,float_shares,as_of,source``. ``as_of`` and ``source`` are
    optional, and ``#`` comment lines are stripped before parsing so the file
    can explain itself. A malformed row is skipped rather than failing the run
    — a typo in a hand-maintained file should cost one symbol, not the morning.
    """
    if path is None:
        return {}
    file = Path(path)
    if not file.exists():
        return {}
    lines = [
        line
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    overrides: dict[str, FloatFact] = {}
    for row in csv.DictReader(lines):
        symbol = str(row.get("symbol") or "").strip().upper()
        raw = str(row.get("float_shares") or "").strip().replace(",", "").replace("_", "")
        if not symbol or not raw:
            continue
        try:
            shares = float(raw)
        except ValueError:
            continue
        if shares <= 0:
            continue
        source = str(row.get("source") or "").strip()
        overrides[symbol] = FloatFact(
            symbol=symbol,
            shares=shares,
            basis=f"override ({source})" if source else "operator override",
            as_of=(str(row.get("as_of") or "").strip() or None),
            is_upper_bound=False,
        )
    return overrides


class FloatProvider(FundamentalsProvider):
    """Fetches cover-page float facts from SEC EDGAR, with a local cache.

    Subclasses :class:`FundamentalsProvider` for its ticker directory, its
    throttled HTTP, and its SEC-required User-Agent handling; the metric set
    is entirely different, so nothing else is shared.

    The cache exists because these facts change a few times a year while this
    screener runs every day. Only the raw filed facts are cached — the share
    count derived from them depends on a price, so it is recomputed each run.
    """

    name = "sec-edgar-float"

    def __init__(
        self,
        *,
        timeout: int = 30,
        max_workers: int = 4,
        overrides_path: str | Path | None = None,
        cache_path: str | Path | None = None,
        cache_days: int = 7,
        max_age_days: int = 400,
        today: date | None = None,
    ) -> None:
        super().__init__(timeout=timeout, max_workers=max_workers)
        self.overrides = load_overrides(overrides_path)
        self.cache_path = Path(cache_path) if cache_path else None
        self.cache_days = max(0, cache_days)
        self.max_age_days = max(1, max_age_days)
        self.today = today or datetime.now(UTC).date()

    # ------------------------------------------------------------- caching

    def _read_cache(self) -> dict[str, dict[str, Any]]:
        if self.cache_path is None or not self.cache_path.exists():
            return {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        entries = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            return {}
        cutoff = time.time() - self.cache_days * 86_400
        return {
            symbol: entry
            for symbol, entry in entries.items()
            if isinstance(entry, dict) and float(entry.get("cached_at", 0)) >= cutoff
        }

    def _write_cache(self, entries: dict[str, dict[str, Any]]) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(
                    {
                        "source": self.name,
                        "written_at": datetime.now(UTC).isoformat(),
                        "symbols": entries,
                    },
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            # A cache that cannot be written must not cost the run its data.
            pass

    # ---------------------------------------------------------------- SEC

    def _concept(self, cik: int, tag: str) -> tuple[tuple[date, float], ...]:
        """One dei concept as ``(measured_on, value)`` pairs, oldest first."""
        payload = self._get_json(COMPANY_CONCEPT_URL.format(cik=cik, tag=tag), f"dei/{tag}")
        rows: dict[date, float] = {}
        for unit, entries in (payload.get("units") or {}).items():
            if unit not in ("shares", "USD"):
                continue
            for entry in entries:
                end, value = entry.get("end"), entry.get("val")
                if not end or value is None:
                    continue
                try:
                    rows[date.fromisoformat(end)] = float(value)
                except (TypeError, ValueError):
                    continue
        return tuple(sorted(rows.items()))

    def _raw_facts(self, symbol: str, cik: int) -> _RawFacts:
        found: dict[str, tuple[date, float] | None] = {}
        notes: list[str] = []
        for tag in (PUBLIC_FLOAT_TAG, SHARES_OUTSTANDING_TAG):
            try:
                series = self._concept(cik, tag)
            except ProviderError as error:
                found[tag] = None
                notes.append(f"{tag}: {error}")
                continue
            found[tag] = series[-1] if series else None
        public = found.get(PUBLIC_FLOAT_TAG)
        shares = found.get(SHARES_OUTSTANDING_TAG)
        return _RawFacts(
            public_float_usd=None if public is None else public[1],
            public_float_as_of=None if public is None else public[0].isoformat(),
            shares_outstanding=None if shares is None else shares[1],
            shares_outstanding_as_of=None if shares is None else shares[0].isoformat(),
            note="; ".join(notes) or None,
        )

    # ------------------------------------------------------------ deriving

    def _fresh(self, stamp: str | None) -> bool:
        """True when a filed date is recent enough to describe today's float."""
        if not stamp:
            return False
        try:
            measured = date.fromisoformat(stamp)
        except ValueError:
            return False
        return (self.today - measured).days <= self.max_age_days

    def _derive(self, symbol: str, facts: _RawFacts, prices: PriceLookup | None) -> FloatFact:
        """Turn filed facts plus a price into a share count, or ``None``.

        Freshness is decided here rather than downstream, because the two facts
        age at very different rates and the choice between them depends on it.
        ``EntityPublicFloat`` is filed once a year and measured two quarters
        before that, so it can be eighteen months old; the share count on a
        10-Q cover page is at most a few. Discarding a symbol because the
        better-quality fact went stale — while a fresh upper bound sat right
        beside it — would reject exactly the names this screener exists to find.
        """
        derived: float | None = None
        derived_as_of: str | None = None
        priced_note: str | None = None

        if (
            facts.public_float_usd
            and facts.public_float_as_of
            and prices is not None
            and self._fresh(facts.public_float_as_of)
        ):
            try:
                measured = date.fromisoformat(facts.public_float_as_of)
            except ValueError:
                measured = None
            if measured is not None:
                found = prices(symbol, measured)
                if found is not None:
                    close, used = found
                    if close > 0 and abs((used - measured).days) <= _PRICE_WINDOW_DAYS:
                        # The split-adjusted close carries every split since the
                        # measurement date, so the quotient is already stated in
                        # today's shares. A raw close would be off by exactly the
                        # split ratio — and reverse splits are routine here.
                        derived = facts.public_float_usd / close
                        derived_as_of = facts.public_float_as_of
                    elif close > 0:
                        priced_note = (
                            "no close within a week of the float measurement date; "
                            "public float was not converted to shares"
                        )
                else:
                    priced_note = (
                        "no price history covers the float measurement date; "
                        "public float was not converted to shares"
                    )

        outstanding = (
            facts.shares_outstanding if self._fresh(facts.shares_outstanding_as_of) else None
        )
        if derived is not None:
            note = priced_note
            shares, upper = derived, False
            basis = f"public float ${facts.public_float_usd:,.0f} ÷ close on {derived_as_of}"
            if outstanding and outstanding < derived:
                # Float cannot exceed shares outstanding; when it appears to,
                # the more recent share count is the better answer.
                shares, upper = outstanding, True
                basis = "shares outstanding (below the derived float; float cannot exceed it)"
                derived_as_of = facts.shares_outstanding_as_of
            elif outstanding and outstanding > derived * _DILUTION_FACTOR:
                note = (
                    f"shares outstanding are {outstanding / derived:.1f}x the derived float as of "
                    f"{facts.shares_outstanding_as_of}; the float has probably grown since"
                )
            return FloatFact(
                symbol=symbol,
                shares=round(shares, 0),
                basis=basis,
                as_of=derived_as_of,
                is_upper_bound=upper,
                note=note,
            )

        if outstanding:
            return FloatFact(
                symbol=symbol,
                shares=round(outstanding, 0),
                basis="shares outstanding (upper bound on float)",
                as_of=facts.shares_outstanding_as_of,
                is_upper_bound=True,
                note=priced_note or facts.note,
            )
        stale = [
            f"{label} last filed {stamp}"
            for label, value, stamp in (
                ("public float", facts.public_float_usd, facts.public_float_as_of),
                ("shares outstanding", facts.shares_outstanding, facts.shares_outstanding_as_of),
            )
            if value and stamp
        ]
        return FloatFact(
            symbol=symbol,
            note=priced_note
            or (
                f"every filed float fact is older than {self.max_age_days} days "
                f"({'; '.join(stale)})"
                if stale
                else facts.note or "no cover-page float facts filed"
            ),
        )

    # ------------------------------------------------------------- public

    def fetch(
        self, symbols: Sequence[str], prices: PriceLookup | None = None
    ) -> tuple[dict[str, FloatFact], list[str]]:
        """Float facts per symbol, plus human-readable warnings.

        ``prices`` supplies the split-adjusted close near a past date; without
        it, ``EntityPublicFloat`` cannot be converted and the result falls back
        to shares outstanding.
        """
        clean = [symbol.upper() for symbol in dict.fromkeys(symbols) if symbol]
        warnings: list[str] = []
        results: dict[str, FloatFact] = {}

        overridden = [symbol for symbol in clean if symbol in self.overrides]
        for symbol in overridden:
            results[symbol] = self.overrides[symbol]
        remaining = [symbol for symbol in clean if symbol not in results]
        if not remaining:
            return results, warnings

        cache = self._read_cache()
        cached = {symbol: cache[symbol] for symbol in remaining if symbol in cache}
        to_fetch = [symbol for symbol in remaining if symbol not in cached]

        try:
            mapping = self.ticker_map()
        except ProviderError as error:
            for symbol in remaining:
                entry = cached.get(symbol)
                results[symbol] = (
                    self._derive(symbol, _RawFacts.from_dict(entry["facts"]), prices)
                    if entry
                    else FloatFact(symbol=symbol, note=f"SEC ticker directory unavailable: {error}")
                )
            warnings.append(
                f"SEC ticker directory unavailable ({error}); float came from cache and "
                "overrides only."
            )
            return results, warnings

        unlisted = [symbol for symbol in to_fetch if symbol not in mapping]
        listed = [symbol for symbol in to_fetch if symbol in mapping]
        for symbol in unlisted:
            results[symbol] = FloatFact(
                symbol=symbol,
                note="no SEC CIK for this ticker (foreign issuer, ETF, or trust)",
            )

        fetched: dict[str, _RawFacts] = {}
        if listed:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                for symbol, facts in zip(
                    listed,
                    pool.map(lambda item: self._raw_facts(item, mapping[item]), listed),
                    strict=True,
                ):
                    fetched[symbol] = facts

        for symbol, facts in fetched.items():
            results[symbol] = self._derive(symbol, facts, prices)
        for symbol, entry in cached.items():
            results[symbol] = self._derive(symbol, _RawFacts.from_dict(entry["facts"]), prices)

        if fetched:
            now = time.time()
            fresh = {
                symbol: {"cached_at": now, "facts": facts.to_dict()}
                for symbol, facts in fetched.items()
            }
            self._write_cache({**cache, **fresh})

        unknown = [symbol for symbol, fact in results.items() if fact.shares is None]
        if unknown:
            warnings.append(
                f"No filed float for {len(unknown)} of {len(clean)} symbols: "
                f"{', '.join(sorted(unknown)[:10])}" + ("…" if len(unknown) > 10 else "")
            )
        if unlisted:
            warnings.append(
                f"{len(unlisted)} symbols have no SEC CIK, so no float could be filed for them: "
                f"{', '.join(unlisted[:10])}" + ("…" if len(unlisted) > 10 else "")
            )
        return results, warnings
