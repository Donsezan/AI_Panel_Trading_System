# Full local gate: format, lint, types, tests, coverage. CI runs the same steps.
# Usage: .\check.ps1 [-Fix]
param([switch]$Fix)

$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv missing - see README quick start" }

function Step($name, [scriptblock]$body) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $body
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; exit 1 }
}

if ($Fix) {
    Step "format"  { & $py -m ruff format . }
    Step "lint"    { & $py -m ruff check --fix . }
} else {
    Step "format"  { & $py -m ruff format --check . }
    Step "lint"    { & $py -m ruff check . }
}
Step "types"    { & $py -m mypy tradebot }
Step "tests"    { & $py -m pytest --cov --cov-report=term-missing:skip-covered --cov-report=json }
Step "coverage" { & $py scripts\coverage_gate.py }

Write-Host "`nall checks passed" -ForegroundColor Green
