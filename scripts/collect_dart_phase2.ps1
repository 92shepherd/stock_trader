# =============================================================
# DART Phase 2 backfill — daily incremental run
# =============================================================
# Run this once per day (e.g. via Windows Task Scheduler) to
# progressively backfill dart_financials and dart_indicators
# from 2020 onward, capped at ~9,000 API calls per run to stay
# under the personal DART limit of 10,000/day.
#
# Resume is automatic: combinations already collected are skipped.
# Usage:
#   .\scripts\collect_dart_phase2.ps1
#
# To override the call cap or start year:
#   .\scripts\collect_dart_phase2.ps1 -MaxCalls 5000 -StartYear 2022
# =============================================================

param(
    [int]$MaxCalls = 9000,
    [int]$StartYear = 2020
)

$ErrorActionPreference = "Stop"

# Move to the project root regardless of where the script is invoked from
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "=== DART Phase 2 backfill ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"
Write-Host "Start year:   $StartYear"
Write-Host "Max calls:    $MaxCalls"
Write-Host ""

python -m src.pipelines.collect_dart `
    --financials --indicators `
    --start-year $StartYear `
    --max-calls $MaxCalls `
    --skip-disclosures `
    --skip-corp-codes

if ($LASTEXITCODE -ne 0) {
    Write-Host "Pipeline exited with code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
