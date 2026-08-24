# California Premarket Scanner

An evidence-first US equity scanner that builds a focused premarket watchlist at
**06:00 California time** every weekday. It filters measurable liquidity,
volatility, relative-volume, spread, and technical-structure criteria; enriches
survivors with catalysts; then emits JSON, CSV, Markdown, and a polished HTML
report with a complete pre-trade checklist.

> **Research only, not investment advice.** A scanner can reduce a universe; it
> cannot verify a trade for you. Recheck prices, quotes, news, earnings, levels,
> position size, and market conditions in a live broker feed before trading.

## Criteria encoded

| Requirement | Default implementation |
| --- | --- |
| Liquidity | 20-session average daily volume strictly above 1,000,000 shares |
| Tight spread | Bid/ask spread at or below 0.30% of midpoint |
| Volatility | 14-session ATR is 2â€“5% of price, inclusive |
| Beta | 60-session return beta to SPY strictly above 1.0 |
| Clean trend | Objective moving-average alignment/slope and directional-efficiency score |
| Clear levels | 20-session support/resistance exist with actionable room |
| Catalysts | Recent Alpaca news, upcoming earnings when configured, and/or a measurable gap |
| Price | $1â€“$1,000, inclusive |
| RVOL | Time-adjusted premarket relative volume strictly above 1.5 |
| Revenue growth | Latest quarter vs the same quarter a year earlier, from SEC filings |
| Leverage | Long-term debt to shareholders' equity at or below the configured cap |
| Volume confirmation | Recent mean volume at or above the configured multiple of the equally long window before it |
| Watchlist | Ranked, never padded, capped at 30; fewer than 10 is reported honestly |
| Checklist | Generated thesis, stop, target, and dollar/share risk for every result |

All thresholds live in [`config/scanner.toml`](config/scanner.toml). A symbol
must pass **every hard gate**, including evidence of at least one catalyst
(recent news, an upcoming earnings event, or a â‰¥2% gap). Missing optional
earnings data remains unknown rather than being treated as â€œno earnings.â€

### Double confirmation

Passing many gates is not the same as being confirmed twice. Momentum and the
moving-average trend are both computed from the same closes, so clearing both
is closer to one signal than two. Volume confirmation is the independent second
leg: it compares recent mean volume against the equally long window before it,
which answers whether participation is expanding while price moves, or whether
the move is running on a thinning book.

The lookback defaults to 5 sessions rather than matching the 63-session
momentum window, because a confirmation only helps at the horizon actually
being traded. Set `min_volume_confirmation = 0.0` to disable the gate and keep
the ratio as a reported column on every row.

### High-volatility list

A separate daily list, screened on liquidity and ATR only, reported **beside**
the watchlist and never inside it. Those names have not passed the hard gates
and carry no thesis, stop, or target, so folding them into the watchlist would
break the guarantee that every name there was fully vetted.

It exists for the mornings when nothing qualifies. The hard gates reject
volatile names for reasons a short-hold trader may not care about â€” no filed
revenue growth, no news catalyst, a trend that is not â€œcleanâ€ â€” and this keeps
those names visible without presenting them as vetted. Names that appear on
both lists are flagged `on_watchlist` rather than silently duplicated. Disable
it with `volatility_list_enabled = false`.

## Quick start

Python 3.11+ required.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

Export the two Alpaca values from `.env` in your shell, then:

```bash
.venv/bin/market-scanner scan --provider alpaca --output-dir artifacts
```

No credentials yet? Exercise the complete pipeline deterministically:

```bash
.venv/bin/market-scanner scan --provider demo --output-dir artifacts
open artifacts/market-scan.html
```

Or scan **real** prices with no account at all, accepting two unverifiable
gates (see [Yahoo feed](#yahoo-feed) before relying on it):

```bash
.venv/bin/market-scanner scan --provider yahoo --output-dir artifacts
```

You can override the seed universe:

```bash
.venv/bin/market-scanner scan --provider alpaca --symbols AMD,NVDA,TSLA,PLTR
```

To hunt the whole listed market for this morning's low-float explosions
instead, see [BlowingStocksScreener](#blowingstocksscreener):

```bash
.venv/bin/market-scanner blowing-stocks
```

## Outputs

Each successful run writes:

- `artifacts/market-scan.json` â€” stable machine-readable result and run metadata
- `artifacts/market-scan.csv` â€” ranked watchlist for spreadsheet workflows
- `artifacts/market-scan.md` â€” concise human review
- `artifacts/market-scan.html` â€” responsive, self-contained dashboard

Runs that produce a high-volatility list also write
`artifacts/market-scan-volatility.csv`. It has its own columns rather than
sharing the watchlist's, so a spreadsheet cannot sort gated and ungated names
into one column by accident. When the list is empty the file is not written,
and any file left from a previous run is removed rather than left to look like
today's.

Outputs include data timestamps, warnings, applied criteria, raw metrics,
catalyst evidence, technical levels, and the pre-trade checklist. Provider or
data failures produce an explicit non-zero exit; they never silently fall back
from live to demo data.

## Fundamentals (SEC EDGAR)

Momentum answers "is it moving?"; fundamentals answer "is the business behind
it growing?" Pass `--fundamentals` (or set `use_fundamentals = true`) to gate
and rank on figures read from **SEC EDGAR XBRL company facts** â€” the filed
record, free and keyless:

```bash
market-scanner scan --provider yahoo --fundamentals
```

Derived per symbol: latest-quarter revenue growth YoY, TTM revenue, TTM net
margin, diluted EPS growth YoY, long-term debt/equity, and ROE. The ranking
blends the fundamental score into the technical score at 35%.

Set `SEC_CONTACT_EMAIL` so requests carry a contact address, as SEC's access
policy asks. Non-filers (ETFs, most ADRs, foreign issuers) have no XBRL facts;
their fundamental gates are left unevaluated rather than failed.

Three rules keep the numbers honest, each of which corrects a way this can
silently go wrong:

- **Abandoned tags are never read as current.** Filers migrate tags mid-history
  â€” NVIDIA stopped tagging `RevenueFromContractWithCustomerâ€¦` in 2022 â€” so tag
  selection prefers what the filer currently uses, not a fixed preference list.
- **Partial line items never stand in for the total.** Where competing tags are
  components of one line (SoFi tags a $154M contract-revenue piece alongside
  its $1.2B net-of-interest total), the widest current tag wins.
- **Ratios require aligned periods.** A margin is computed only when its income
  and revenue windows close within 45 days of each other, so a current profit
  is never divided by a stale revenue series.

A metric the filings do not support stays `None` and is counted in `missing`;
`coverage` reports what share was actually available, and the fundamental score
is rescaled across only the components that were measurable, so partial
coverage is not scored as weakness.

## Multibagger report

A **separate** report answering a different question: which universe names have
already multiplied.

```bash
market-scanner multibagger --output-dir artifacts/multibagger
```

It is deliberately not part of the watchlist, for two reasons:

- **Horizon.** Every scanner gate â€” RVOL, ATR, the gap â€” measures a single
  session. A multibagger is a multi-year return, and the scan path truncates
  history to `history_days`, so it cannot measure one.
- **Contract.** The scan's guarantee is that every listed name cleared every
  hard gate. Admitting multibaggers would need an OR-path for names that never
  passed RVOL, quietly making that guarantee false.

Thresholds live under `[multibagger]` in `config/scanner.toml`. Each horizon a
listing is too young to cover is omitted rather than reported as zero, so a
recent IPO never appears to have a three-year record.

**The one-year column is the one to read.** A multiple describes what a stock
did, not where it is going, and several past multibaggers are currently
falling â€” PLTR is 11x over three years and down over the last one. Every row
carries a `still_rising` flag for exactly this reason. Nothing in this report
is predictive.

## Scheduling the multibagger report

`scripts/run_multibagger.ps1` writes to `artifacts/multibagger/` and logs to
`artifacts/logs/run-multibagger.log`. Because the report reads completed daily
closes, it does not care whether the market is open.

Register it on Windows for 06:25 daily:

```powershell
$proj = "C:\Claude\marketscanner\market-scanner"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$proj\scripts\run_multibagger.ps1`"" `
  -WorkingDirectory $proj
Register-ScheduledTask -TaskName "MarketScanner-Multibagger" -Action $action `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 6:25am) `
  -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable)
```

`-ExecutionPolicy Bypass` is required because the machine policy is
`Restricted`; it applies to that one process and changes no system setting.

## BlowingStocksScreener

A **third** report, and the only one that goes looking for the whole market.
Run it with:

```bash
market-scanner blowing-stocks
```

The scan above reads a curated 245-name universe hunting a clean, liquid
setup. This hunts the opposite kind of name: a sub-$20 stock with a
sub-20-million-share float running many times its normal volume on a headline.
Those names are not on anybody's list the day before, so the universe is
**discovered each run** â€” roughly 6,200 listed US common stocks from the
Nasdaq Trader symbol directory, with ETFs, test issues, warrants, rights and
units removed.

Two screens run side by side and are reported separately, because a name can
legitimately appear on one, the other, or both:

| | Screen A â€” low-float momentum | Screen B â€” catalyst breakout |
| --- | --- | --- |
| Price | $0.50 â€“ $20 | $1 â€“ $50 |
| Float | <20M (best <10M, parabolic <5M) | not gated |
| RVOL | >5 (explosive >10, parabolic >20) | >3 |
| Gap | >20% (best >30%) | not gated |
| Volume | >500K, or >200K premarket | >2x the 20-session mean |
| News | has news; keywords rank it | keyword match **required** |
| Breakout | > premarket high **and** > VWAP | > yesterday's high **and** > 50-SMA |

Keywords are FDA, Patent, Reverse split, AI, Partnership, Earnings beat, plus
Phase 2/Phase 3 and Guidance raise on screen B. They are matched as patterns,
not substrings: "beats estimates" counts as an earnings beat, and `AI` is
matched case-sensitively so `said`, `maintain` and `Air` do not.

Every threshold lives under `[blowing_stocks]` in `config/scanner.toml`. The
`best_*` and `parabolic_*` values gate nothing â€” they label how far past the
gate a name got. Each row is tiered by its **weakest** dimension, so a 3M float
at RVOL 6 reads "standard", not "parabolic".

### How it runs in two stages

Measuring seven thousand symbols properly is not affordable; measuring the
twenty that are moving is trivial. So:

1. **Prefilter.** One batched snapshot request per 500 symbols returns last
   price, today's volume, and the previous session's bar â€” about fifteen
   requests for the entire market. Anything outside the price band, or not
   moving on either gap or volume, stops here.
2. **Measurement.** Only the survivors (capped at `max_deep_scan_symbols`,
   default 120, ordered by how hard they are moving) get daily history,
   five-minute bars, news, and SEC float.

A full run takes about twenty seconds.

### Float

Float is the gate that defines screen A, and no free price feed publishes it.
It is rebuilt from cover-page facts every filer is required to state:

- **`EntityPublicFloat`** â€” the market value of stock held by non-affiliates,
  in dollars, on a stated date. Divided by the **split-adjusted** close on that
  date it becomes a share count in today's terms. The split adjustment is not
  incidental: one reverse split, routine in this universe, throws the answer
  off by exactly the split ratio.
- **`EntityCommonStockSharesOutstanding`** â€” used when the first is stale or
  unpriceable. It is not float; it is an **upper bound** on float, and rows
  derived from it are marked with a dagger. A name that passes on that basis
  genuinely passed; one rejected on it may still have a small float.

`config/float_overrides.csv` beats both â€” put a real float figure there and it
wins. A symbol whose float cannot be proven is **rejected**, not passed; run
with `--allow-unproven-float` to keep those names with the gate flagged
instead, which the report then says loudly on every affected row.

### Feeds

The default is `delayed_sip`: the full consolidated tape, fifteen minutes late.
`iex` is real-time but carries only IEX's own executions â€” a few percent of
volume â€” so a 500,000-share gate measured on it is not measuring what it
claims. The two Alpaca endpoints spell that entitlement differently, and the
screener translates: snapshots ask for `delayed_sip`, bars ask for `sip`.

### Nothing unmeasured ever passes

The same rule as the scanner, applied harder. Every required gate except float
**rejects** a symbol it cannot measure, and the report counts why:

```
rejections low_float: gap (18), above_premarket_high (15), rvol (14), float_unproven (9)
```

A "measured zero" and a "missing measurement" are kept apart â€” a symbol that
did not trade in the window has a volume of zero, while a symbol the feed
returned nothing for is unmeasured and drops out.

One consequence is worth knowing. Inside the premarket, "price > premarket
high" is compared against the high **through the previous completed bar**;
after the bell it is the settled 04:00â€“09:29 level. A running high already
contains the price being tested against it, so reading it the same way on both
sides of the open would make that gate impossible to pass before 09:30.

## Scheduling the screener â€” 06:33 daily

`scripts/run_blowing_stocks.ps1` writes to `artifacts/blowing-stocks/` and logs
to `artifacts/logs/run-blowing-stocks.log`. Register the daily task with:

```powershell
.\scripts\install_blowing_stocks_task.ps1
```

06:33 Pacific is **09:33 Eastern**, three minutes after the opening bell. With
the fifteen-minute delayed tape the measurement window closes near 09:18 ET, so
the run screens the completed premarket session while the opening drive is
still live.

It runs **every day**, weekends included. On a day the market did not open, one
batched request establishes there is no session and the report says so â€” the
gap, RVOL and breakout gates all describe a live session, so there is nothing
to screen and no reason to spend the requests finding that out.

### A week of history

Every run writes today's report twice: the current file, and a dated copy under
`artifacts/blowing-stocks/history/`. Anything older than
`history_retention_days` (default **7**) is deleted on the next run, so the
directory always holds the last week and never grows.

```
artifacts/blowing-stocks/
â”œâ”€â”€ blowing-stocks.{json,csv,md,html}      today
â”œâ”€â”€ float-cache.json                       filed float facts, reused for a week
â””â”€â”€ history/
    â”œâ”€â”€ blowing-stocks-2026-08-13.{json,csv,md,html}
    â””â”€â”€ â€¦ through 2026-08-19
```

The archive is stamped with the **run** date, not the session date. A Saturday
run reports Friday's session; stamping it Friday would overwrite Friday's own
report with a market-closed page.

Override the window per run with `--retention-days`, or permanently in
`config/scanner.toml`.

## Data and methodology boundaries

- **Universe:** `config/universe.txt` is a curated liquid starting set, not the
  full US market. Replace or pass `--symbols` for broader coverage. Every symbol
  still faces the same gates.
- **Alpaca feed:** defaults to IEX for broad account compatibility. IEX quotes
  reflect one venue and may understate consolidated volume or differ from NBBO.
  Use SIP only when your subscription permits it.
- <a id="yahoo-feed"></a>**Yahoo feed:** needs no credentials and supplies real
  prices, adjusted daily OHLCV, and headlines, but Yahoo's public API publishes
  **no bid/ask quote** and reports **zero extended-hours volume**. Two hard
  gates therefore cannot be measured:

  | Gate | Alpaca | Yahoo |
  | --- | --- | --- |
  | Tight spread | measured | **never evaluated** â€” no bid/ask is published |
  | RVOL | measured | **not evaluated premarket**; intraday runs measure regular-session volume instead |

  The scanner omits an unmeasurable gate rather than scoring it as a pass, and
  reports it as `NOT VERIFIED` in the filters, `null` in JSON/CSV, and `â€”` in
  the tables. A Yahoo watchlist has cleared fewer requirements than an Alpaca
  one, so at 06:00 PT it cannot confirm the premarket volume surge the scanner
  exists to find. Yahoo's API is also unofficial, rate-limited, and can change
  without notice. Use Alpaca for the intended premarket workflow.
- **06:00 PT timing:** 09:00 ET on normal US trading days. RVOL compares
  cumulative extended-hours volume through the same time-of-day across prior
  sessions where data is available; inspect the report's method/warnings.
- **Catalysts are headlines, not verified events.** The scanner requires a
  recent headline the provider ties to the symbol, an upcoming earnings date
  when configured, or a measurable gap. It does not classify materiality, so a
  provider-tagged market-wide story can still satisfy the gate. Only the gap
  catalyst is independently measured. Read the headline before acting on it.
- **Fundamentals lag by design.** Filings describe a closed fiscal period, not
  today. `max_fundamentals_age_days` rejects periods older than the configured
  window, and every row reports the period end it was derived from.
- **A confirmed move is not a likely one.** Volume confirmation filters signals
  that fire on thinning participation. It does not predict direction, and every
  confirmation layer delays entry, which widens the stop and forfeits part of
  the move. It trades a lower false-signal rate for a worse entry price.
- **The high-volatility list is not vetted.** It clears liquidity and ATR and
  nothing else â€” no catalyst, no trend quality, no fundamentals, no trade plan.
  A wide range is as available to a loss as to a gain.
- **Beta:** historical estimate, not a stable property or forecast.
- **Levels/trend:** deterministic heuristics, not visual certainty. They create
  review candidates, not trade instructions.
- **Earnings:** optional Finnhub calendar enrichment requires
  `FINNHUB_API_KEY`. Unknown data remains unknown; it is not rendered as â€œnone.â€
- **Holidays/stale data:** the weekday schedule can run on exchange holidays.
  Check `data_as_of` and warnings before acting.

Alpaca's snapshot endpoint supplies latest trade/quote/daily bars; historical
bars supply indicator history, and its news endpoint supplies headlines. See
[Alpaca Market Data documentation](https://docs.alpaca.markets/docs/about-market-data-api).

## 06:00 California scheduling

The included GitHub Actions workflow uses:

```yaml
- cron: "0 6 * * 1-5"
  timezone: "America/Los_Angeles"
```

That remains 06:00 through PST/PDT changes. Add `APCA_API_KEY_ID` and
`APCA_API_SECRET_KEY` under **Settings â†’ Secrets and variables â†’ Actions**, then
enable the workflow. GitHub schedules are best-effort and can be delayed or
dropped under load; [`docs/SCHEDULING.md`](docs/SCHEDULING.md) covers manual
runs (including credential-free demo validation), exact operational limits,
Docker, and a local macOS `launchd` alternative.

## Quality checks

```bash
.venv/bin/ruff check .
.venv/bin/pytest --cov=market_scanner --cov-report=term-missing
```

Tests are deterministic and never call live services. CI runs lint and tests on
every push and pull request.

## Architecture

```text
provider (Alpaca/Yahoo/demo)
  â†’ normalized quotes, daily/minute bars, news, earnings
  â†’ hard eligibility gates (a gate the provider cannot measure is
    reported unverified, never assumed to pass)
  â†’ technical/catalyst scoring
  â†’ rank + cap (never relax/pad)
  â†’ JSON / CSV / Markdown / HTML
```

The screener is a parallel pipeline sharing none of the scan's gates, because
it answers a different question over a different universe:

```text
symbol directory (Nasdaq Trader)  â†’ ~6,200 listed common stocks
  â†’ batched snapshots â†’ prefilter (price band + actually moving)
  â†’ per-survivor: daily bars, 5-minute bars, news, SEC cover-page float
  â†’ two independent screens (an unmeasured gate rejects, never passes)
  â†’ rank + cap per screen (never padded)
  â†’ JSON / CSV / Markdown / HTML  +  a 7-day dated archive
```

Provider I/O, calculations, scanner decisions, and rendering are separate
modules so each can be tested or replaced independently. The screener follows
the same split: `blowing_sources` fetches, `float_data` reconstructs float,
`blowing_stocks` decides, `blowing_reporting` renders and prunes.

## License

MIT. See [`LICENSE`](LICENSE).
