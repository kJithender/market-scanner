<#
.SYNOPSIS
    Runs the market scanner on this machine and opens the report.

.DESCRIPTION
    GitHub's scheduler drifts by up to two hours, which pushes the 06:00
    premarket scan past the 06:30 Pacific open. Relative volume is measured
    over a window that closes at 09:29 ET, so a late run reports the same RVOL
    against a drifted price. Running here fires on time.

    Live Alpaca data requires APCA_API_KEY_ID and APCA_API_SECRET_KEY in the
    environment. Set them once as user environment variables:

        [Environment]::SetEnvironmentVariable("APCA_API_KEY_ID", "<id>", "User")
        [Environment]::SetEnvironmentVariable("APCA_API_SECRET_KEY", "<secret>", "User")

    Output goes to artifacts\local\, which is git-ignored, so it never collides
    with the reports\ directory synced from GitHub.
#>
[CmdletBinding()]
param(
    [ValidateSet("alpaca", "yahoo", "demo")]
    [string]$Provider = "yahoo",
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$outputDir = Join-Path $projectDir "artifacts\local"
$logDir = Join-Path $projectDir "artifacts\logs"
foreach ($dir in @($outputDir, $logDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
}
$log = Join-Path $logDir "run-scan.log"

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

    Write-Log "Scanning with provider '$Provider'"
    $summary = & $python -m market_scanner scan --provider $Provider --output-dir $outputDir 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Log "FAILED (exit $code): $($summary -join ' ')"
        exit $code
    }

    $parsed = $summary | Out-String | ConvertFrom-Json
    Write-Log ("Scanned {0}, qualified {1}." -f $parsed.scanned, $parsed.qualified)
    foreach ($warning in $parsed.warnings) { Write-Log "  warning: $warning" }

    $report = Join-Path $outputDir "market-scan.html"
    if (-not $NoOpen -and (Test-Path $report)) {
        Start-Process $report
        Write-Log "Opened $report"
    }
}
catch {
    Write-Log "FAILED: $($_.Exception.Message)"
    exit 1
}
