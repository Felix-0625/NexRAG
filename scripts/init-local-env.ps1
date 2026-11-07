# 创建被 Git 忽略的本机环境文件，各服务使用独立密钥。
# 已有 .env 文件不覆盖，也不输出其内容。
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$examplePath = Join-Path $projectRoot '.env.example'
$targetPath = Join-Path $projectRoot '.env'
if (Test-Path -LiteralPath $targetPath) {
    Write-Host 'Local .env already exists; left it unchanged.'
    exit 0
}

$content = [System.IO.File]::ReadAllText($examplePath)
$generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    foreach ($name in @(
        'NEXRAG_SHARED_API_KEY',
        'NEXRAG_USER_JWT_SECRET',
        'WEBUI_SECRET_KEY',
        'INGESTION_API_KEY',
        'RETRIEVAL_API_KEY'
    )) {
        $bytes = New-Object byte[] 32
        $generator.GetBytes($bytes)
        $secret = -join ($bytes | ForEach-Object { $_.ToString('x2') })
        $pattern = '(?m)^' + [regex]::Escape($name) + '=$'
        if (-not [regex]::IsMatch($content, $pattern)) {
            throw "Missing secret setting in .env.example: $name"
        }
        $content = [regex]::Replace($content, $pattern, "$name=$secret")
    }
} finally {
    $generator.Dispose()
}

$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($targetPath, $content, $utf8)
Write-Host 'Created ignored local .env with independent service secrets.'
