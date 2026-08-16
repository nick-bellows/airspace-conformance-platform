# Local mirror of the CI quality gate. Run this before pushing; if it passes,
# the `quality` workflow will too. Keep the two in lockstep -- a local script
# that drifts from CI is worse than no local script.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "No .venv found. Create one with:" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e `".[all]`""
    exit 1
}

$targets = @("src", "tests", "scripts", "eval", "conftest.py")

Write-Host "`n[1/5] ruff check" -ForegroundColor Cyan
& $python -m ruff check @targets
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n[2/5] ruff format --check" -ForegroundColor Cyan
& $python -m ruff format --check @targets
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n[3/5] mypy --strict" -ForegroundColor Cyan
& $python -m mypy src
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Integration, e2e, and perf need Docker and minutes, so the default gate stays
# fast and offline. CI runs them as separate jobs; see the footer below.
Write-Host "`n[4/5] pytest (unit + contract)" -ForegroundColor Cyan
& $python -m pytest tests/unit tests/contract --cov=acp --cov-report=term-missing
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n[5/5] contract drift (Kafka schemas + OpenAPI)" -ForegroundColor Cyan
& $python scripts/contracts.py --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`nAll checks passed." -ForegroundColor Green
Write-Host ""
Write-Host "Not run here (Docker, minutes each - CI runs them as separate jobs):" -ForegroundColor DarkGray
Write-Host "  pytest tests/integration   real Kafka, Postgres, Redis via testcontainers" -ForegroundColor DarkGray
Write-Host "  pytest tests/e2e           the whole stack under docker compose" -ForegroundColor DarkGray
Write-Host "  pytest tests/perf -s       the latency budget at 500 aircraft" -ForegroundColor DarkGray
