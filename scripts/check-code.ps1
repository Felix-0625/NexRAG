# 使用当前检出安装的依赖，执行与 CI 相同的核心门禁。
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
& $pythonExe (Join-Path $PSScriptRoot 'check-repository.py')
if ($LASTEXITCODE -ne 0) { throw 'Repository or documentation check failed.' }
& $pythonExe -m ruff check src scripts tests
if ($LASTEXITCODE -ne 0) { throw 'Ruff lint failed.' }
& $pythonExe -m ruff format --check src scripts tests
if ($LASTEXITCODE -ne 0) { throw 'Ruff format check failed.' }
# 隐私测试直接执行补丁后的 WebUI 函数；先验证生成检出，避免源码缺失削弱测试范围。
& (Join-Path $PSScriptRoot 'prepare-webui.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Pinned WebUI patch preparation failed.' }
& $pythonExe -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw 'Core tests failed.' }
if ($IncludeWebUI) {
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCommand) { throw 'Install Node.js 22 and run npm ci in webui before -IncludeWebUI.' }
    & $nodeCommand.Source (Join-Path $PSScriptRoot 'check-webui.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Patched Svelte compilation failed.' }
    Push-Location (Join-Path $projectRoot 'webui')
    try {
        & $nodeCommand.Source --max-old-space-size=6144 node_modules/vite/bin/vite.js build
        if ($LASTEXITCODE -ne 0) { throw 'Patched browser build failed.' }
    } finally { Pop-Location }
}
if ($IncludeRagServices) {
    foreach ($service in @('ingestion-service', 'retrieval-agent', 'search-agent')) {
        Push-Location (Join-Path $projectRoot "base/$service")
        try {
            & $uvCommand.Source lock --check
            if ($LASTEXITCODE -ne 0) { throw "$service lock file does not match its configuration." }
            & $uvCommand.Source run --frozen --all-extras --all-groups --python 3.12 pytest -q
            if ($LASTEXITCODE -ne 0) { throw "$service regression tests failed." }
        } finally { Pop-Location }
    }
}
Write-Host 'All requested checks passed. Live model and browser acceptance are separate gates.' -ForegroundColor Green
