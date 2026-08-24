# thinkorswim ports

Two thinkScript studies that reproduce as much of this project's screening as
thinkScript can express.

| File | Ports | Reproduces the Python gate set? |
| --- | --- | --- |
| [`multiscan.thinkscript`](multiscan.thinkscript) | `market-scanner scan` | **No — a looser subset.** See below. |
| [`multibagger.thinkscript`](multibagger.thinkscript) | `market-scanner multibagger` | Yes, in full. |

## Installing

1. **Scan tab → Stock Hacker → Add Study Filter**
2. Click the pencil icon on the new filter, then **thinkScript**
3. Delete the placeholder, paste the file's contents, **OK**
4. Set the scan **aggregation period to `Day`** — both scripts read daily bars
5. Set the filter comparison to `is true`

For a watchlist column instead of a filter, use the column variants at the
bottom of each file: **Watchlist → Customize → Custom → scripted column**.

## What the multiscan port cannot do

`multiscan.thinkscript` returns a **looser** set than `market-scanner scan`.
Four gates have no thinkScript equivalent:

- **Fundamentals** — revenue growth, debt/equity, net margin. thinkScript has
  no access to SEC XBRL data and there is no TOS equivalent.
- **P/E** — not available to scripts, but the Stock Hacker UI has it:
  *Add Filter → Stock → P/E ratio*. Add it alongside the study filter.
- **Premarket RVOL** — the Python gate compares cumulative premarket volume
  against the same clock time on prior sessions. A daily-aggregation scan
  cannot see inside the session. The two volume measures that *are* ported
  (volume confirmation, volume spike) cover the reproducible part.
- **Bid/ask spread and news catalysts** — unavailable to scan scripts.

Treat anything the TOS scan surfaces as still needing those checks. The full
scanner remains the stricter tool.

## Verification

The ported formulas were checked against the Python indicators by simulating
thinkScript's semantics (`close[n]`, `Average`, `Sum`) over the same bars for
NVDA, MU, PLTR, GE and TOST. Momentum, volume confirmation, volume spike,
trend slope, path efficiency, support, resistance and beta all matched to six
decimal places.

Two caveats on that:

- **ATR%** matched in numerator but not denominator: the Python scanner uses
  completed sessions and the live price, a TOS daily scan includes the forming
  bar in both. Intraday they differ slightly; after the close they converge.
- **The scripts have not been run inside thinkorswim.** Verification covers the
  arithmetic, not TOS syntax acceptance. If a line is rejected, the likely
  culprits are `close("SPY")` in the beta block (secondary-symbol references
  are permitted in scans but slow them) and `GetValue` offsets on symbols with
  short history.

## History requirements

`multibagger.thinkscript` reads **756 daily bars** back for its 3-year horizon.
If thinkorswim has not loaded that much history the horizon evaluates to 0 and
the name is judged on shorter horizons only — it fails safe, but quietly.
Reduce `horizon3Years` if results look thin.

## Keeping thresholds in sync

Both scripts hard-code their defaults as `input` values copied from
[`../config/scanner.toml`](../config/scanner.toml). They do **not** read that
file. Changing a threshold there does not change the TOS scan; update the
matching `input` by hand.
