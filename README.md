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
| Volatility | 14-session ATR is 2–5% of price, inclusive |
| Beta | 60-session return beta to SPY strictly above 1.0 |
| Clean trend | Objective moving-average alignment/slope and directional-efficiency score |
| Clear levels | 20-session support/resistance exist with actionable room |
| Catalysts | Recent Alpaca news, upcoming earnings when configured, and/or a measurable gap |
| Price | $1–$1,000, inclusive |
| RVOL | Time-adjusted premarket relative volume strictly above 1.5 |
| Watchlist | Ranked, never padded, capped at 30; fewer than 10 is reported honestly |
| Checklist | Generated thesis, stop, target, and dollar/share risk for every result |

All thresholds live in [`config/scanner.toml`](config/scanner.toml). A symbol
must pass **every hard gate**, including evidence of at least one catalyst
(recent news, an upcoming earnings event, or a ≥2% gap). Missing optional
earnings data remains unknown rather than being treated as “no earnings.”

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

## Outputs

Each successful run writes:

- `artifacts/market-scan.json` — stable machine-readable result and run metadata
- `artifacts/market-scan.csv` — ranked watchlist for spreadsheet workflows
- `artifacts/market-scan.md` — concise human review
- `artifacts/market-scan.html` — responsive, self-contained dashboard

Outputs include data timestamps, warnings, applied criteria, raw metrics,
catalyst evidence, technical levels, and the pre-trade checklist. Provider or
data failures produce an explicit non-zero exit; they never silently fall back
from live to demo data.

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
  | Tight spread | measured | **never evaluated** — no bid/ask is published |
  | RVOL | measured | **not evaluated premarket**; intraday runs measure regular-session volume instead |

  The scanner omits an unmeasurable gate rather than scoring it as a pass, and
  reports it as `NOT VERIFIED` in the filters, `null` in JSON/CSV, and `—` in
  the tables. A Yahoo watchlist has cleared fewer requirements than an Alpaca
  one, so at 06:00 PT it cannot confirm the premarket volume surge the scanner
  exists to find. Yahoo's API is also unofficial, rate-limited, and can change
  without notice. Use Alpaca for the intended premarket workflow.
- **06:00 PT timing:** 09:00 ET on normal US trading days. RVOL compares
  cumulative extended-hours volume through the same time-of-day across prior
  sessions where data is available; inspect the report's method/warnings.
- **Beta:** historical estimate, not a stable property or forecast.
- **Levels/trend:** deterministic heuristics, not visual certainty. They create
  review candidates, not trade instructions.
- **Earnings:** optional Finnhub calendar enrichment requires
  `FINNHUB_API_KEY`. Unknown data remains unknown; it is not rendered as “none.”
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
`APCA_API_SECRET_KEY` under **Settings → Secrets and variables → Actions**, then
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
  → normalized quotes, daily/minute bars, news, earnings
  → hard eligibility gates (a gate the provider cannot measure is
    reported unverified, never assumed to pass)
  → technical/catalyst scoring
  → rank + cap (never relax/pad)
  → JSON / CSV / Markdown / HTML
```

Provider I/O, calculations, scanner decisions, and rendering are separate
modules so each can be tested or replaced independently.

## License

MIT. See [`LICENSE`](LICENSE).
