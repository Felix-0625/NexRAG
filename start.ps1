# Windows 启动入口：在项目内准备 uv，无需安装全局 Python 或 Node。
param(
    [switch]$NoBrowser,
    [switch]$SkipModels,
    [switch]$PrepareOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot '.local-run'
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
$env:UV_CACHE_DIR = Join-Path $runtimeRoot 'cache/uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $runtimeRoot 'python'
$env:PYTHONIOENCODING = 'utf-8'

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCommand) {
    $uvExe = $uvCommand.Source
} else {
    $uvDir = Join-Path $runtimeRoot 'tools/uv'
    $uvExe = Join-Path $uvDir 'uv.exe'
    if (-not (Test-Path -LiteralPath $uvExe)) {
        Write-Host 'Preparing the local Python package manager (first run only)...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $version = '0.11.15'
        $assetName = 'uv-x86_64-pc-windows-msvc.zip'
        $release = Invoke-RestMethod "https://api.github.com/repos/astral-sh/uv/releases/tags/$version"
        $asset = $release.assets | Where-Object name -eq $assetName
        if (-not $asset -or $asset.digest -notmatch '^sha256:[a-f0-9]{64}$') {
            throw 'Official uv release is missing its SHA256 digest.'
        }
        $archive = Join-Path $runtimeRoot 'uv.zip'
        Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $archive
        $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $asset.digest.Substring(7)) { throw 'uv download checksum mismatch.' }
        Expand-Archive -LiteralPath $archive -DestinationPath $uvDir -Force
        if (-not (Test-Path -LiteralPath $uvExe)) { throw 'uv.exe is missing from the downloaded archive.' }
    }
}

$launchArgs = @('run', '--no-project', '--python', '3.12', '--with', 'python-dotenv==1.2.4',
    (Join-Path $projectRoot 'scripts/launch_local.py'), '--uv', $uvExe)
if ($NoBrowser) { $launchArgs += '--no-browser' }
if ($SkipModels) { $launchArgs += '--skip-models' }
if ($PrepareOnly) { $launchArgs += '--prepare-only' }
& $uvExe @launchArgs
exit $LASTEXITCODE
