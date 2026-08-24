<#
.SYNOPSIS
    Runs the BlowingStocksScreener and keeps a week of dated reports.

.DESCRIPTION
    Intended for the daily 06:33 Pacific scheduled task, which is 09:33 ET â€”
    three minutes after the opening bell. With the default delayed_sip feed the
    tape arrives fifteen minutes late, so the measurement window closes around
    09:18 ET and the run screens the full premarket session: the gap, the
    premarket high, premarket VWAP, and relative volume against the same
    window on prior mornings.

    Unlike the 06:15 scan, this one discovers its own universe. It pulls every
    listed US common stock from the Nasdaq Trader symbol directory, prefilters
    the whole market from batched snapshots, and only then measures the few
    dozen names that are actually moving.

    Live data requires APCA_API_KEY_ID and APCA_API_SECRET_KEY in the
    environment. Set them once as user environment variables:

        [Environment]::SetEnvironmentVariable("APCA_API_KEY_ID", "<id>", "User")
        [Environment]::SetEnvironmentVariable("APCA_API_SECRET_KEY", "<secret>", "User")

    Float comes from SEC EDGAR, which asks callers to identify themselves.
    Set SEC_CONTACT_EMAIL to your own address so the requests are attributable:

        [Environment]::SetEnvironmentVariable("SEC_CONTACT_EMAIL", "<you@example.com>", "User")

    Output goes to artifacts\blowing-stocks\, which is git-ignored. Today's
    report sits at the top level; history\ holds one dated copy per run day and
    anything older than the retention window is deleted on the next run.

    The screener runs every day, including weekends. On a day the market did
    not open it says so and screens nothing: the gap, RVOL and breakout gates
    all describe a live session.
#>
[CmdletBinding()]
param(
    [ValidateSet("alpaca", "demo")]
    [string]$Provider = "alpaca",
    [ValidateSet("delayed_sip", "iex", "sip")]
    [string]$Feed = "delayed_sip",
    [int]$RetentionDays = 0,
    [switch]$Open
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$outputDir = Join-Path $projectDir "artifacts\blowing-stocks"
$logDir = Join-Path $projectDir "artifacts\logs"
foreach ($dir in @($outputDir, $logDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
}
$log = Join-Path $logDir "run-blowing-stocks.log"

function Write-Log([string]$message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Output $line
}

try {
    Set-Location $projectDir
    # Windows Application Control can block the generated console-script shim.
    # The interpreter is trusted, so invoke the package as a module instead.
    $python = Join-Path $projectDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        throw "market-scanner is not installed. Run: python -m venv .venv; .venv\Scripts\python.exe -m pip install -e ."
    }

    # Fail before calling out rather than emitting a confusing provider error.
    if ($Provider -eq "alpaca") {
        $missing = @("APCA_API_KEY_ID", "APCA_API_SECRET_KEY") |
            Where-Object { -not [Environment]::GetEnvironmentVariable($_) }
        if ($missing) {
            Write-Log "SKIPPED: missing $($missing -join ', '). Set them as user environment variables; see this script's header."
            exit 2
        }
    }

    $arguments = @("blowing-stocks", "--provider", $Provider, "--output-dir", $outputDir)
    if ($Provider -eq "alpaca") { $arguments += @("--feed", $Feed) }
    if ($RetentionDays -gt 0) { $arguments += @("--retention-days", $RetentionDays) }

    Write-Log "Screening with provider '$Provider' feed '$Feed'"
    $summary = & $python -m market_scanner @arguments 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Log "FAILED (exit $code): $($summary -join ' ')"
        exit $code
    }

    $parsed = $summary | Out-String | ConvertFrom-Json
    Write-Log ("Session {0} ({1}); universe {2}, measured {3}; low-float {4}, catalyst {5}." -f `
        $parsed.session_date, $parsed.session_phase, $parsed.universe, $parsed.measured, `
        $parsed.low_float, $parsed.catalyst)
    if ($parsed.pruned) { Write-Log ("Pruned {0} expired archive files." -f $parsed.pruned.Count) }
    foreach ($warning in $parsed.warnings) { Write-Log "  warning: $warning" }

    $report = Join-Path $outputDir "blowing-stocks.html"
    if ($Open -and (Test-Path $report)) {
        Start-Process $report
        Write-Log "Opened $report"
    }
}
catch {
    Write-Log "FAILED: $($_.Exception.Message)"
    exit 1
}
