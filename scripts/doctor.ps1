# Diagnostic tool for inspecting NexRAG environment, dependencies, services, and assets.
$ErrorActionPreference = 'Continue'

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "            NexRAG Local Environment Doctor              " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host ""

$issues = 0
$warnings = 0

function Report-Pass($msg) {
    Write-Host "  [PASS] $msg" -ForegroundColor Green
}

function Report-Warn($msg, $tip = "") {
    $script:warnings++
    Write-Host "  [WARN] $msg" -ForegroundColor Yellow
    if ($tip) {
        Write-Host "         Tip: $tip" -ForegroundColor DarkYellow
    }
}

function Report-Fail($msg, $fix = "") {
    $script:issues++
    Write-Host "  [FAIL] $msg" -ForegroundColor Red
    if ($fix) {
        Write-Host "         Fix: $fix" -ForegroundColor DarkRed
    }
}

# 1. Check Python and Virtual Environment
Write-Host "1. Checking Python Environment..." -ForegroundColor White
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Test-Path $pythonExe) {
    $pyVer = & $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    Report-Pass "Found local venv Python $pyVer ($pythonExe)"
} else {
    Report-Warn "Local .venv not found; checking global Python..." "Run 'python -m venv .venv' to create a local virtualenv"
    $pythonExe = 'python'
}

# 2. Check Configuration & Secrets
Write-Host "`n2. Checking Local Environment Files and Secrets..." -ForegroundColor White
$envPath = Join-Path $projectRoot '.env'
if (Test-Path $envPath) {
    Report-Pass "Found local .env configuration file"
    $envContent = Get-Content $envPath -Raw
    $keys = @('NEXRAG_SHARED_API_KEY', 'NEXRAG_USER_JWT_SECRET', 'INGESTION_API_KEY', 'RETRIEVAL_API_KEY')
    foreach ($k in $keys) {
        if ($envContent -match "(?m)^$k=(.+)$") {
            $val = $matches[1].Trim()
            if ($val.Length -ge 32) {
                Report-Pass "Secret $k is properly configured (length >= 32)"
            } else {
                Report-Fail "Secret $k is shorter than 32 characters" "Run 'powershell scripts/init-local-env.ps1' to generate secure keys"
            }
        } else {
            Report-Fail "Missing secret $k in .env" "Run 'powershell scripts/init-local-env.ps1'"
        }
    }
} else {
    Report-Fail "Missing .env file" "Run 'powershell -File scripts/init-local-env.ps1' to create it"
}

# 3. Check Data Assets
Write-Host "`n3. Checking Data Assets and Business Configuration..." -ForegroundColor White
$dbPath = Join-Path $projectRoot 'data\sqlite\business.db'
if (Test-Path $dbPath) {
    $dbSize = (Get-Item $dbPath).Length / 1MB
    Report-Pass ("SQLite business database exists ({0:N2} MB)" -f $dbSize)
} else {
    Report-Warn "SQLite business database not found at $dbPath" "Run 'powershell scripts/fetch-olist.ps1' then 'python scripts/import_olist.py'"
}

$yamlConfigs = @(
    'examples/olist/business-source.yaml',
    'examples/olist/metadata.yaml',
    'examples/olist/tenant-access.yaml'
)
foreach ($cfg in $yamlConfigs) {
    $fullCfg = Join-Path $projectRoot $cfg
    if (Test-Path $fullCfg) {
        Report-Pass "Found $cfg"
    } else {
        Report-Fail "Missing $cfg"
    }
}

# 4. Check External Services and Ports
Write-Host "`n4. Checking Microservices and Ports..." -ForegroundColor White

$runtimePath = Join-Path $projectRoot '.local-run/runtime.json'
$runtime = if (Test-Path -LiteralPath $runtimePath) { Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json } else { $null }
function Get-ServicePort($service, $fallback) {
    if ($runtime -and $runtime.ports -and $runtime.ports.$service) { return [int]$runtime.ports.$service }
    return $fallback
}
$ollamaBase = 'http://127.0.0.1:11434'
$trustPath = Join-Path $projectRoot '.local-run/ollama-trust.json'
if (Test-Path -LiteralPath $trustPath) {
    $trust = Get-Content -LiteralPath $trustPath -Raw | ConvertFrom-Json
    if ($trust.endpoint) { $ollamaBase = $trust.endpoint }
}

function Test-PortConnection($port, $name, $hint) {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect('127.0.0.1', $port, $null, $null)
        $wait = $iar.AsyncWaitHandle.WaitOne(800, $false)
        if ($wait -and $tcp.Connected) {
            $tcp.EndConnect($iar)
            $tcp.Close()
            Report-Pass "Service $name (port $port) is reachable"
            return $true
        }
    } catch {}
    Report-Warn "Service $name (port $port) is not reachable" $hint
    return $false
}

$ollamaOnline = Test-PortConnection ([uri]$ollamaBase).Port "Ollama LLM" "Run '.\start.cmd' to prepare a verified local-only model server"
if ($ollamaOnline) {
    try {
        $cloudStatus = Invoke-RestMethod -Uri "$ollamaBase/api/status" -TimeoutSec 2
        if ($cloudStatus.cloud.disabled -eq $true) { Report-Pass 'Ollama explicitly reports cloud features disabled' }
        else { Report-Fail 'Ollama local-only status is not verified' 'Use start.cmd; a loopback URL alone is insufficient' }
        $tags = (Invoke-RestMethod -Uri "$ollamaBase/api/tags" -TimeoutSec 2).models
        $modelNames = $tags | ForEach-Object { $_.name }
        if ($modelNames -contains "qwen3:4b-instruct" -or $modelNames -match "qwen3") {
            Report-Pass "Ollama has Qwen instruction model installed"
        } else {
            Report-Warn "Qwen instruction model not found in Ollama" "Run 'ollama pull qwen3:4b-instruct'"
        }
        if ($modelNames -contains "bge-m3" -or $modelNames -match "bge-m3") {
            Report-Pass "Ollama has BGE-M3 embedding model installed"
        } else {
            Report-Warn "BGE-M3 model not found in Ollama" "Run 'ollama pull bge-m3'"
        }
    } catch {
        Report-Warn "Could not inspect Ollama installed models"
    }
}

Test-PortConnection (Get-ServicePort 'qdrant' 6333) "Qdrant Vector DB" "Run '.\start.cmd'" | Out-Null
Test-PortConnection (Get-ServicePort 'ingestion' 8001) "Ingestion Service" "Run '.\start.cmd'" | Out-Null
Test-PortConnection (Get-ServicePort 'retrieval' 8002) "Retrieval Service" "Run '.\start.cmd'" | Out-Null
Test-PortConnection (Get-ServicePort 'nexrag' 8000) "NexRAG Backend" "Run '.\start.cmd'" | Out-Null
Test-PortConnection (Get-ServicePort 'web' 3000) "Open WebUI Frontend" "Run '.\start.cmd'" | Out-Null

# Summary
Write-Host "`n----------------------------------------------------------" -ForegroundColor Cyan
if ($issues -eq 0 -and $warnings -eq 0) {
    Write-Host "Diagnostic Complete: Everything is in optimal condition!" -ForegroundColor Green
} elseif ($issues -eq 0) {
    Write-Host "Diagnostic Complete: No blocking errors, $warnings warning(s) found." -ForegroundColor Yellow
} else {
    Write-Host "Diagnostic Complete: $issues blocking error(s) and $warnings warning(s) detected." -ForegroundColor Red
}
Write-Host "----------------------------------------------------------`n"
