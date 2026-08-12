<#
.SYNOPSIS
    Fetches the latest published market-scan report into this working copy.

.DESCRIPTION
    The scheduled GitHub Actions run publishes the report to reports/ on the
    default branch. This script pulls that commit down and, unless -NoOpen is
    passed, opens the HTML dashboard.

    It never runs the scanner itself, so no market-data credentials are needed
    on this machine.
#>
[CmdletBinding()]
param(
    [string]$Remote = "personal",
    [string]$Branch = "main",
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $projectDir "artifacts\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$log = Join-Path $logDir "pull-report.log"

function Write-Log([string]$message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Output $line
}

try {
    Set-Location $projectDir
    Write-Log "Fetching $Remote/$Branch"
    git fetch $Remote $Branch --quiet
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed with exit code $LASTEXITCODE" }

    # Fast-forward only: never clobber local work in progress.
    git merge --ff-only "$Remote/$Branch" --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Log "Cannot fast-forward (local commits or conflicts). Report not updated."
        exit 1
    }

    $report = Join-Path $projectDir "reports\market-scan.html"
    if (-not (Test-Path $report)) {
        Write-Log "No report found at reports\market-scan.html yet."
        exit 0
    }

    # GitHub's scheduler can lag by hours, so this task repeats through the
    # morning. Identify the report by the commit that published it and act only
    # when that changes, so repeat runs stay silent instead of reopening tabs.
    $publishedSha = (git log -1 --format=%H -- reports).Trim()
    $stateFile = Join-Path $logDir ".last-seen-report"
    $lastSeen = if (Test-Path $stateFile) { (Get-Content $stateFile -Raw).Trim() } else { "" }

    if ($publishedSha -eq $lastSeen) {
        Write-Log "Report unchanged since last check ($($publishedSha.Substring(0,7))). Waiting."
        exit 0
    }

    $publishedAt = [datetime](git log -1 --format=%cI -- reports).Trim()
    $ageHours = ((Get-Date) - $publishedAt).TotalHours
    Write-Log ("New report {0} published {1:N1} h ago." -f $publishedSha.Substring(0, 7), $ageHours)
    Set-Content -Path $stateFile -Value $publishedSha -Encoding utf8

    if ($ageHours -gt 18) {
        Write-Log "WARNING: report is over 18 hours old; the scheduled scan may not have run."
    }

    if (-not $NoOpen) {
        Start-Process $report
        Write-Log "Opened the dashboard."
    }
}
catch {
    Write-Log "FAILED: $($_.Exception.Message)"
    exit 1
}
