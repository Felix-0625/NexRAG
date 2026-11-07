# 审计锁定的运行和开发依赖，只发送依赖信息，不发送应用或用户内容。
param([ValidateSet('all', 'core', 'ingestion', 'retrieval', 'search', 'webui')][string]$Scope = 'all')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$uv = Get-Command uv -ErrorAction Stop
$output = Join-Path $root '.local-run/audits'
New-Item -ItemType Directory -Force $output | Out-Null
$scopes = if ($Scope -eq 'all') { @('core', 'ingestion', 'retrieval', 'search', 'webui') } else { @($Scope) }
foreach ($item in $scopes) {
    $requirements = Join-Path $output "$item-requirements.txt"
    if ($item -eq 'webui') {
        Copy-Item -LiteralPath (Join-Path $root 'scripts/webui-requirements.lock') -Destination $requirements
    } else {
        $project = switch ($item) {
            'ingestion' { Join-Path $root 'base/ingestion-service' }
            'retrieval' { Join-Path $root 'base/retrieval-agent' }
            'search' { Join-Path $root 'base/search-agent' }
            default { $root }
        }
        & $uv.Source export --project $project --frozen --all-extras --all-groups --no-hashes --no-emit-project --format requirements-txt --output-file $requirements | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to export the frozen $item dependencies." }
    }
    $report = Join-Path $output "$item.json"
    & $uv.Source tool run --from 'pip-audit==2.10.1' pip-audit -r $requirements --no-deps --disable-pip --format json --output $report
    if ($LASTEXITCODE -ne 0) { throw "$item dependency audit failed. See $report" }
    Write-Host "$item dependency audit passed: $report"
}
