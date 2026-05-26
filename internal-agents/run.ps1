<#
.SYNOPSIS
    Windows replacement for Makefile targets.
    Usage: .\run.ps1 [target]
    Example: .\run.ps1 install
             .\run.ps1 test
#>

param([string]$Target = "help", [int]$Port = 0)

$PYTHON = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Ensure-Env {
    $envFile     = Join-Path $PSScriptRoot ".env"
    $exampleFile = Join-Path $PSScriptRoot ".env.example"
    if (-not (Test-Path $envFile)) {
        Copy-Item $exampleFile $envFile
        Write-Host "Created .env from .env.example -- edit it before using real credentials." -ForegroundColor Yellow
    }
    # Always load .env values — overrides stale values from previous runs in the same session
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
            $key   = $matches[1].Trim()
            $value = $matches[2].Trim()
            [System.Environment]::SetEnvironmentVariable($key, $value, 'Process')
        }
    }
    # Guarantee ANTHROPIC_API_KEY has a value so Settings() does not fail in mock mode
    if (-not $env:ANTHROPIC_API_KEY) {
        $env:ANTHROPIC_API_KEY = "sk-ant-mock-dev-placeholder"
    }
}

function Invoke-Install {
    Write-Host "Installing dependencies..." -ForegroundColor Cyan
    pip install uv
    python -m uv venv
    python -m uv pip install -e ".[dev]" --python (Join-Path $PSScriptRoot ".venv")
}

function Invoke-Env {
    $envFile     = Join-Path $PSScriptRoot ".env"
    $exampleFile = Join-Path $PSScriptRoot ".env.example"
    if (Test-Path $envFile) {
        Write-Host ".env already exists -- skipping" -ForegroundColor Gray
    }
    else {
        Copy-Item $exampleFile $envFile
        Write-Host "Created .env -- edit it before running with real credentials." -ForegroundColor Yellow
    }
}

function Get-Port {
    param([int]$Default = 8000)
    # -Port flag takes precedence, then PORT env var, then default
    if ($script:Port -gt 0)          { return $script:Port }
    if ($env:PORT -match '^\d+$')    { return [int]$env:PORT }
    return $Default
}

function Assert-PortFree {
    param([int]$Port)
    $inUse = netstat -ano | Select-String ":$Port\s.*LISTENING"
    if ($inUse) {
        $ownerPid = ($inUse -split '\s+' | Where-Object { $_ -match '^\d+$' })[-1]
        Write-Host ""
        Write-Host "ERROR: Port $Port is already in use (PID $ownerPid)." -ForegroundColor Red
        Write-Host ""
        Write-Host "  Option A — kill it from an admin PowerShell:" -ForegroundColor Yellow
        Write-Host "      taskkill /F /PID $ownerPid" -ForegroundColor White
        Write-Host ""
        Write-Host "  Option B — use a different port (no admin needed):" -ForegroundColor Yellow
        Write-Host "      .\run.ps1 $script:Target -Port 8001" -ForegroundColor White
        Write-Host ""
        exit 1
    }
}

function Invoke-Dev {
    Ensure-Env
    $port = Get-Port -Default 8000
    Assert-PortFree -Port $port
    $mockLabel = if ($env:MOCK_MODE -eq "false") { "live" } else { "mock" }
    Write-Host "Starting API in $mockLabel mode with live reload at http://localhost:$port ..." -ForegroundColor Cyan
    Write-Host "  MOCK_MODE=$env:MOCK_MODE  (set in .env to change)" -ForegroundColor Gray
    & $PYTHON -m uvicorn src.api.main:app --port $port --reload --log-level info --timeout-keep-alive 30
}

function Invoke-DevDebug {
    Ensure-Env
    $port = Get-Port -Default 8000
    Assert-PortFree -Port $port
    $mockLabel = if ($env:MOCK_MODE -eq "false") { "live" } else { "mock" }
    Write-Host "Starting API in $mockLabel/debug mode at http://localhost:$port ..." -ForegroundColor Cyan
    Write-Host "  MOCK_MODE=$env:MOCK_MODE  (set in .env to change)" -ForegroundColor Gray
    & $PYTHON -m uvicorn src.api.main:app --port $port --reload --log-level debug --timeout-keep-alive 30
}

function Invoke-Start {
    Ensure-Env
    $port = Get-Port -Default 8000
    Assert-PortFree -Port $port
    Write-Host "Starting API with real credentials at http://0.0.0.0:$port ..." -ForegroundColor Cyan
    & $PYTHON -m uvicorn src.api.main:app --host 0.0.0.0 --port $port --timeout-keep-alive 30
}

function Invoke-Test {
    Write-Host "Running full test suite..." -ForegroundColor Cyan
    & $PYTHON -m pytest -v
}

function Invoke-TestGateway {
    & $PYTHON -m pytest tests/test_gateway.py -v
}

function Invoke-TestAgents {
    & $PYTHON -m pytest tests/test_agents.py -v
}

function Invoke-TestE2E {
    & $PYTHON -m pytest tests/test_e2e.py -v
}

function Invoke-StreamDemo {
    Ensure-Env
    & $PYTHON scripts/ws_demo.py
}

function Invoke-Help {
    Write-Host ""
    Write-Host "Usage: .\run.ps1 [target] [-Port <number>]" -ForegroundColor Yellow
    Write-Host ""
    $targets = @(
        [pscustomobject]@{ Name = "install";       Desc = "Install all dependencies (including dev)" },
        [pscustomobject]@{ Name = "env";           Desc = "Copy .env.example to .env (skips if .env exists)" },
        [pscustomobject]@{ Name = "dev";           Desc = "Start the API with live reload (MOCK_MODE from .env)" },
        [pscustomobject]@{ Name = "dev-debug";     Desc = "Start the API with debug logging (MOCK_MODE from .env)" },
        [pscustomobject]@{ Name = "start";         Desc = "Start the API using .env credentials (real mode)" },
        [pscustomobject]@{ Name = "test";          Desc = "Run the full test suite" },
        [pscustomobject]@{ Name = "test-gateway";  Desc = "Run gateway tests only" },
        [pscustomobject]@{ Name = "test-agents";   Desc = "Run agent unit tests only" },
        [pscustomobject]@{ Name = "test-e2e";      Desc = "Run end-to-end pipeline tests only" },
        [pscustomobject]@{ Name = "stream-demo";   Desc = "Run WebSocket streaming demo" }
    )
    foreach ($t in $targets) {
        Write-Host ("  {0,-16} {1}" -f $t.Name, $t.Desc) -ForegroundColor Cyan
    }
    Write-Host ""
}

Set-Location $PSScriptRoot

switch ($Target) {
    "install"      { Invoke-Install }
    "env"          { Invoke-Env }
    "dev"          { Invoke-Dev }
    "dev-debug"    { Invoke-DevDebug }
    "start"        { Invoke-Start }
    "test"         { Invoke-Test }
    "test-gateway" { Invoke-TestGateway }
    "test-agents"  { Invoke-TestAgents }
    "test-e2e"     { Invoke-TestE2E }
    "stream-demo"  { Invoke-StreamDemo }
    "help"         { Invoke-Help }
    default        { Write-Host "Unknown target '$Target'. Run: .\run.ps1 help" -ForegroundColor Red }
}
