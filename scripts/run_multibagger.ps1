<#
.SYNOPSIS
    Generates the historical multibagger report and logs the outcome.

.DESCRIPTION
    Intended for the daily 06:25 scheduled task. Unlike the premarket scan,
    this report reads completed daily closes, so it does not care whether the
    market is open â€” running it before the bell is fine.

    The report is historical: every multiple in it has already happened. It is
    not a watchlist and not a forecast.

    Output goes to AllScreenersResults\, shared with the other two screeners
    and git-ignored. Every report is stamped with today's date (DD-MM-YYYY),
    so a run here never overwrites yesterday's file.
#>
[CmdletBinding()]
param(
    [double]$MinMultiple = 0,
    [switch]$Open
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$outputDir = Join-Path $projectDir "AllScreenersResults"
$logDir = Join-Path $projectDir "artifacts\logs"
foreach ($dir in @($outputDir, $logDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
}
$log = Join-Path $logDir "run-multibagger.log"

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

    $arguments = @("multibagger", "--output-dir", $outputDir)
    if ($MinMultiple -gt 0) { $arguments += @("--min-multiple", $MinMultiple) }

    Write-Log "Generating multibagger report"
    $summary = & $python -m market_scanner @arguments 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Log "FAILED (exit $code): $($summary -join ' ')"
        exit $code
    }

    $parsed = $summary | Out-String | ConvertFrom-Json
    Write-Log ("Scanned {0}, qualified {1}." -f $parsed.scanned, $parsed.qualified)
    foreach ($warning in $parsed.warnings) { Write-Log "  warning: $warning" }

    # The filename is date-stamped by the CLI itself, so the exact path is
    # read from its own summary rather than reconstructed here.
    $report = $parsed.outputs.html
    if ($Open -and $report -and (Test-Path $report)) {
        Start-Process $report
        Write-Log "Opened $report"
    }
}
catch {
    Write-Log "FAILED: $($_.Exception.Message)"
    exit 1
}
