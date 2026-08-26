# The tool's own gate. The root .\check.ps1 is unmodified and names only `tradebot` in its mypy,
# pytest and coverage steps (spec §2.1) — root `ruff` already walks this folder, so formatting and
# linting are checked in both places and that is deliberate.
# Usage: .\decision_lab\check.ps1 [-Fix]
param([switch]$Fix)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv missing - see README quick start" }

function Step($name, [scriptblock]$body) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $body
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; exit 1 }
}

Push-Location $root
try {
    if ($Fix) {
        Step "format" { & $py -m ruff format decision_lab }
        Step "lint"   { & $py -m ruff check --fix decision_lab }
    } else {
        Step "format" { & $py -m ruff format --check decision_lab }
        Step "lint"   { & $py -m ruff check decision_lab }
    }
    Step "types" { & $py -m mypy decision_lab }
    Step "tests" { & $py -m pytest decision_lab/tests }
} finally {
    Pop-Location
}

Write-Host "`ndecision_lab checks passed" -ForegroundColor Green
