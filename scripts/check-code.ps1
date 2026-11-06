# Run the same core gates as CI, using this checkout's installed dependencies.
param([switch]$IncludeWebUI, [switch]$IncludeRagServices)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Project dependencies are missing. Run uv sync --frozen --group dev first.'
}
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCommand) { throw 'uv is required to validate uv.lock. Install uv and run uv sync --frozen --group dev.' }
Write-Host 'Checking the lock file...' -ForegroundColor Cyan
& $uvCommand.Source lock --check
if ($LASTEXITCODE -ne 0) { throw 'uv.lock does not match pyproject.toml.' }
& $pythonExe -m ruff check src scripts tests
if ($LASTEXITCODE -ne 0) { throw 'Ruff lint failed.' }
& $pythonExe -m ruff format --check src scripts tests
if ($LASTEXITCODE -ne 0) { throw 'Ruff format check failed.' }
# Privacy tests execute the actual patched WebUI functions. Verify the generated
# checkout first, so a missing checkout cannot silently weaken the test suite.
& (Join-Path $PSScriptRoot 'prepare-webui.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Pinned WebUI patch preparation failed.' }
& $pythonExe -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw 'Core tests failed.' }
if ($IncludeWebUI) {
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCommand) { throw 'Install Node.js 22 and run npm ci in webui before -IncludeWebUI.' }
    & $nodeCommand.Source (Join-Path $PSScriptRoot 'check-webui.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Patched Svelte compilation failed.' }
}
if ($IncludeRagServices) {
    foreach ($service in @('ingestion-service', 'retrieval-agent')) {
        Push-Location (Join-Path $projectRoot "base/$service")
        try {
            & $uvCommand.Source run --frozen --extra dev --python 3.12 pytest -q
            if ($LASTEXITCODE -ne 0) { throw "$service regression tests failed." }
        } finally { Pop-Location }
    }
}
Write-Host 'All requested checks passed. Live model and browser acceptance are separate gates.' -ForegroundColor Green
