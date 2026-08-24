# Scheduling and operations

## GitHub Actions: 6:00 AM California time

`.github/workflows/market-scan.yml` runs at **06:00 Monday-Friday** in
`America/Los_Angeles`. GitHub Actions supports an IANA `timezone` alongside the
cron expression, making the schedule daylight-saving-time safe:

```yaml
schedule:
  - cron: "0 6 * * 1-5"
    timezone: "America/Los_Angeles"
```

Scheduled Actions are best-effort. GitHub notes that high load—especially at the
start of an hour—can delay a run and, in extreme cases, drop it. This workflow
keeps the requested exact 06:00 schedule; use the local `launchd` option below
if runner dispatch timing must be controlled by your Mac.

Scheduled workflows run from the latest commit on the default branch. GitHub
may disable schedules after 60 days without repository activity in a public
repository. A manual **Run workflow** trigger is included for recovery/testing.

Official references:

- [GitHub workflow schedule syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule)
- [Schedule event limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## Choosing the scheduled provider

The scheduled run reads the repository **variable** `SCAN_PROVIDER`
(**Settings → Secrets and variables → Actions → Variables**). It defaults to
`alpaca` when unset. Manual runs override it with the workflow-dispatch input.

| `SCAN_PROVIDER` | Credentials | What the 06:00 run proves |
| --- | --- | --- |
| `alpaca` | required | every hard gate, including premarket RVOL and spread |
| `yahoo` | none | all gates **except** spread and premarket RVOL, which Yahoo cannot supply |

At 06:00 PT the market is premarket, and Yahoo publishes no extended-hours
volume, so a `yahoo` schedule cannot confirm the volume surge the scanner
exists to find. Prefer `alpaca` for the intended workflow; see the Yahoo feed
notes in the README before relying on `yahoo`.

## Repository secrets

In **Settings → Secrets and variables → Actions**, add:

| Secret | Required | Purpose |
| --- | --- | --- |
| `APCA_API_KEY_ID` | For `alpaca` | Alpaca market-data API key ID |
| `APCA_API_SECRET_KEY` | For `alpaca` | Alpaca market-data API secret |
| `FINNHUB_API_KEY` | No | Enriches catalyst/news data when supported |

The scan fails with an explicit error when `SCAN_PROVIDER` is `alpaca` and
either key is missing. It never silently falls back to another provider.

The workflow does not grant write access to repository contents. It validates
the two required Alpaca values without printing them, runs the complete test
suite, performs the scan, and retains `artifacts/` as an Actions run artifact for
30 days. Artifact access follows the repository's GitHub permissions. Do not
commit `.env` files or API keys.

## Manual run

Open **Actions → Market scan → Run workflow**. Choose `demo` to validate the
complete cloud workflow without credentials, or `alpaca` for live data. With
GitHub CLI:

```bash
gh workflow run market-scan.yml -f provider=demo
```

Download output from the completed run's **Artifacts** section.

## Local macOS schedule with launchd

`launchd` uses the Mac's local timezone. On a Mac configured for California
time, this follows PST/PDT automatically. Install after creating the virtual
environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
./scripts/install_launchd.sh
```

The generated job runs weekdays at 06:00 and writes logs under
`artifacts/logs/`. The Mac must be awake; macOS may run a missed calendar event
after wake rather than at exactly 06:00.

Credential files are intentionally not generated. Prefer a secrets manager that
injects values before `scripts/run_scan.sh`, or manually add an
`EnvironmentVariables` dictionary to
`~/Library/LaunchAgents/com.market-scanner.daily.plist` and keep its permissions
at `0600`. Required variable names match the GitHub secrets above.

Check and remove the job:

```bash
launchctl print "gui/$(id -u)/com.market-scanner.daily"
launchctl bootout "gui/$(id -u)/com.market-scanner.daily"
rm ~/Library/LaunchAgents/com.market-scanner.daily.plist
```

## Docker

```bash
docker build -t market-scanner .
docker run --rm \
  -e APCA_API_KEY_ID \
  -e APCA_API_SECRET_KEY \
  -e FINNHUB_API_KEY \
  -v "$PWD/artifacts:/app/artifacts" \
  market-scanner
```

The image runs as an unprivileged user. Docker does not provide scheduling by
itself; schedule `docker run` with the host scheduler or use GitHub Actions.

## BlowingStocksScreener: 06:33 daily

`MarketScanner-BlowingStocks` runs `scripts/run_blowing_stocks.ps1` every day
at **06:33** local time. Install or update it with:

```powershell
.\scripts\install_blowing_stocks_task.ps1
```

The time is not arbitrary. 06:33 Pacific is 09:33 Eastern, three minutes past
the opening bell; with the default fifteen-minute delayed consolidated tape the
measurement window closes near 09:18 ET. That is exactly what the screen wants:
the completed premarket session — its gap, its high, its VWAP, its relative
volume — read while the opening drive is still live.

Unlike the 06:00 scan this task is **daily, not weekdays**. A closed-market run
costs one batched request to establish there is no session to screen, and
running every day means the holiday calendar never has to be maintained here.
The report says plainly which it was:

```
"session_phase": "closed",
"warnings": ["US market not open for business at 2026-08-22 09:18 EDT; the newest
              session on the tape is 2026-08-21. Nothing was screened …"]
```

`StartWhenAvailable` is set, so a machine asleep at 06:33 runs the screen when
it wakes. The task does not wake the machine.

### Interaction with the other tasks

| Task | Time | What it reads |
| --- | --- | --- |
| `MarketScanner-LocalScan` | 06:15 weekdays | curated 245-name universe, premarket |
| `MarketScanner-Multibagger` | 06:25 daily | completed daily closes, multi-year |
| `MarketScanner-PullReport` | 06:30 daily | syncs the cloud-published scan |
| `MarketScanner-BlowingStocks` | 06:33 daily | whole listed market, premarket |

They do not collide: the screener starts after the others have finished, and
writes to its own `artifacts/blowing-stocks/` directory.

### Retention

Each run archives a dated copy of all four formats under
`artifacts/blowing-stocks/history/` and deletes anything older than
`history_retention_days` (default 7). Nothing else in that directory is
touched — the prune matches only the exact `blowing-stocks-YYYY-MM-DD.<ext>`
pattern and parses the date before removing a file.

Override per run with `--retention-days N`, or permanently under
`[blowing_stocks]` in `config/scanner.toml`.

### Credentials

Beyond the Alpaca keys the scan already needs, set `SEC_CONTACT_EMAIL` to your
own address. SEC EDGAR asks callers to identify themselves in the User-Agent,
and the float figures the low-float screen depends on come from there:

```powershell
[Environment]::SetEnvironmentVariable("SEC_CONTACT_EMAIL", "<you@example.com>", "User")
```

Without it the requests still work but identify themselves generically, which
the SEC's access policy asks you not to rely on.

### Removing the task

```powershell
Unregister-ScheduledTask -TaskName "MarketScanner-BlowingStocks" -Confirm:$false
```
