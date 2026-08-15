# Bring up the whole stack and open the display.
#
#   .\scripts\demo.ps1                              # head-on conflict (default)
#   .\scripts\demo.ps1 -Scenario quiet-cruise       # the false-alarm control
#   .\scripts\demo.ps1 -Tools                       # plus the Redpanda console
#   .\scripts\demo.ps1 -Down                        # tear everything down

[CmdletBinding()]
param(
    [string]$Scenario = "head-on-conflict",
    [switch]$Tools,
    [switch]$Down
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
$compose = "deploy/compose.yml"

if ($Down) {
    Write-Host "Tearing down (including volumes)..." -ForegroundColor Cyan
    docker compose -f $compose --profile tools down -v
    exit $LASTEXITCODE
}

$scenarioFile = "scenarios/$Scenario.yaml"
if (-not (Test-Path $scenarioFile)) {
    Write-Host "No such scenario: $scenarioFile" -ForegroundColor Red
    Write-Host "Available:" -ForegroundColor Yellow
    Get-ChildItem scenarios -Filter *.yaml | ForEach-Object { "  $($_.BaseName)" }
    exit 1
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker is not running. Start Docker Desktop and try again." -ForegroundColor Red
    exit 1
}

$profileArgs = if ($Tools) { @("--profile", "tools") } else { @() }

Write-Host "Building images..." -ForegroundColor Cyan
docker compose -f $compose @profileArgs build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Starting stack with scenario '$Scenario'..." -ForegroundColor Cyan
$env:ACP_SCENARIO = $scenarioFile
docker compose -f $compose @profileArgs up -d
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# The API healthcheck is the readiness signal for the whole stack: it only
# passes once Redis and the migrations are done.
Write-Host "Waiting for the API..." -NoNewline
for ($i = 0; $i -lt 60; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:8000/health" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) { break }
    } catch {
        Start-Sleep -Seconds 1
        Write-Host "." -NoNewline
    }
}
Write-Host ""

$ready = Invoke-RestMethod -Uri "http://localhost:8000/ready"
if (-not $ready.ready) {
    Write-Host "API is up but not ready: $($ready | ConvertTo-Json -Compress)" -ForegroundColor Yellow
    Write-Host "Check logs with: docker compose -f $compose logs" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "Stack is up." -ForegroundColor Green
Write-Host "  Display        http://localhost:8000"
Write-Host "  API docs       http://localhost:8000/docs"
Write-Host "  Live tracks    http://localhost:8000/v1/tracks"
if ($Tools) { Write-Host "  Kafka console  http://localhost:8080" }
Write-Host ""
Write-Host "Tear down with: .\scripts\demo.ps1 -Down" -ForegroundColor DarkGray

Start-Process "http://localhost:8000"
