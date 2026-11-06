# Prepare the pinned Open WebUI source checkout used by NexRAG.
# The checkout is generated locally; project changes belong in tracked patch files.
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$checkoutPath = Join-Path $projectRoot 'webui'
$upstreamUrl = 'https://github.com/AarhusAI/open-webui.git'
$upstreamTag = 'v0.11.4'
$upstreamCommit = '8bd8b4fac5e059578ac0c74b3c18d11139f88b7d'
$patchRoot = Join-Path $projectRoot 'patches/open-webui/v0.11.4'
$patches = @(
    (Join-Path $patchRoot 'nexrag/tenant-selector.diff'),
    (Join-Path $patchRoot 'nexrag/ops-dashboard.diff'),
    (Join-Path $patchRoot 'nexrag/workspace.diff'),
    (Join-Path $patchRoot 'nexrag/security-npm.diff')
)

function Move-VerifiedWebUISource([string]$repository) {
    $sourceAbsolute = [System.IO.Path]::GetFullPath($repository)
    $targetAbsolute = [System.IO.Path]::GetFullPath($checkoutPath)
    $workspaceAbsolute = [System.IO.Path]::GetFullPath($projectRoot)
    if (-not $sourceAbsolute.StartsWith($workspaceAbsolute + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase) `
        -or $targetAbsolute -ne (Join-Path $workspaceAbsolute 'webui') `
        -or ((Get-Item -LiteralPath $sourceAbsolute -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw 'Refusing to move a checkout outside the intended workspace.'
    }
    # Directory.Move refuses an existing destination instead of merging or
    # overwriting another process's/user's files.
    [System.IO.Directory]::Move($sourceAbsolute, $targetAbsolute)
}

function Initialize-WebUISource {
    # Clone into a same-volume staging directory. A failed/interrupted download
    # must not turn webui/ into a broken checkout that blocks the next start.
    $bootstrapRoot = Join-Path $projectRoot '.local-run/webui-bootstrap'
    New-Item -ItemType Directory -Force -Path $bootstrapRoot | Out-Null
    $unfinished = @(Get-ChildItem -LiteralPath $bootstrapRoot -Directory | Where-Object {
        $marker = Join-Path $_.FullName 'attempt.json'
        if (-not (Test-Path -LiteralPath $marker)) { return $false }
        try {
            $attempt = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
            return $attempt.owner -eq 'nexrag-webui-bootstrap-v1' -and $attempt.status -ne 'completed'
        } catch { return $false }
    })
    # An interruption can happen after download succeeds but before the atomic
    # move. Recover only our own, pristine, exact-pinned staging checkout.
    foreach ($previous in $unfinished) {
        $previousRepository = Join-Path $previous.FullName 'repository'
        if (-not (Test-Path -LiteralPath (Join-Path $previousRepository '.git'))) { continue }
        $previousMetadata = Get-Content -LiteralPath (Join-Path $previous.FullName 'attempt.json') -Raw | ConvertFrom-Json
        if ($previousMetadata.commit -ne $upstreamCommit) { continue }
        if ($previousMetadata.process_id -and (Get-Process -Id $previousMetadata.process_id -ErrorAction SilentlyContinue)) {
            continue
        }
        $ErrorActionPreference = 'Continue'
        $candidateCommit = (& git -C $previousRepository rev-parse --verify HEAD 2>$null) -join ''
        $commitOK = $LASTEXITCODE -eq 0 -and $candidateCommit -eq $upstreamCommit
        $candidateOrigin = (& git -C $previousRepository config --get remote.origin.url 2>$null) -join ''
        $originOK = $LASTEXITCODE -eq 0 -and $candidateOrigin -eq $upstreamUrl
        $candidateStatus = @(& git -C $previousRepository status --porcelain --untracked-files=all 2>$null)
        $clean = $LASTEXITCODE -eq 0 -and $candidateStatus.Count -eq 0
        $ErrorActionPreference = 'Stop'
        if ($commitOK -and $originOK -and $clean) {
            Move-VerifiedWebUISource $previousRepository
            $previousMetadata.status = 'completed'
            $previousMetadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $previous.FullName 'attempt.json') -Encoding UTF8
            Write-Host 'Recovered the previously downloaded, verified WebUI source.'
            return
        }
    }
    if ($unfinished.Count -ge 3) {
        throw "Three incomplete WebUI downloads were retained at $bootstrapRoot. Inspect/archive those attempts before retrying; no existing checkout was deleted."
    }
    $attemptRoot = Join-Path $bootstrapRoot ('attempt-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $attemptRoot | Out-Null
    $repository = Join-Path $attemptRoot 'repository'
    $metadataPath = Join-Path $attemptRoot 'attempt.json'
    $metadata = @{ owner = 'nexrag-webui-bootstrap-v1'; status = 'starting'; commit = $upstreamCommit }
    $metadata | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8
    $clone = $null
    try {
        # These options affect this public clone only, never the user's global
        # Git/proxy/TLS configuration. A stalled transfer aborts after 120s;
        # the total Git process deadline is eight minutes.
        $arguments = @('-c', 'http.lowSpeedLimit=1024', '-c', 'http.lowSpeedTime=120',
                       'clone', '--depth', '1', '--branch', $upstreamTag, '--', $upstreamUrl,
                       ('"{0}"' -f $repository))
        $clone = Start-Process -FilePath (Get-Command git).Source -ArgumentList $arguments `
            -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $attemptRoot 'git.stdout.log') `
            -RedirectStandardError (Join-Path $attemptRoot 'git.stderr.log')
        # Windows PowerShell 5 can lose ExitCode after WaitForExit unless its
        # process handle is retained before waiting. Do not treat null as zero.
        $null = $clone.Handle
        $metadata.status = 'downloading'
        $metadata.process_id = $clone.Id
        $metadata | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8
        $timer = [System.Diagnostics.Stopwatch]::StartNew()
        $lastProgress = 0
        while (-not $clone.WaitForExit(1000)) {
            if ($timer.Elapsed.TotalSeconds -ge 480) {
                # Only the Git process created immediately above and its own
                # children belong to this timeout. Unrelated processes survive.
                & taskkill.exe /PID $clone.Id /T /F | Out-Null
                $clone.WaitForExit()
                throw "WebUI source download exceeded eight minutes. Check the network and retry. Details retained at $attemptRoot"
            }
            if ($timer.Elapsed.TotalSeconds -ge $lastProgress + 20) {
                Write-Host 'Downloading the pinned WebUI source; the first run depends on network speed...'
                $lastProgress = $timer.Elapsed.TotalSeconds
            }
        }
        $clone.WaitForExit()
        if ($clone.ExitCode -ne 0) {
            throw "WebUI source download failed (Git exit $($clone.ExitCode)). Check network access and retry. Details retained at $attemptRoot"
        }
        $downloadedCommit = (git -C $repository rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $downloadedCommit -ne $upstreamCommit) {
            throw "Downloaded WebUI revision does not match the pinned commit; checkout retained at $attemptRoot"
        }
        Move-VerifiedWebUISource $repository
        $metadata.status = 'completed'
        $metadata | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8
    } catch {
        $metadata.status = 'failed'
        $metadata | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8
        throw
    } finally {
        if ($clone) {
            try {
                if (-not $clone.HasExited) {
                    & taskkill.exe /PID $clone.Id /T /F 2>$null | Out-Null
                    $clone.WaitForExit(10000) | Out-Null
                }
            } finally { $clone.Dispose() }
        }
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $checkoutPath '.git'))) {
    if (Test-Path -LiteralPath $checkoutPath) {
        throw "The checkout path exists but is not a Git repository: $checkoutPath"
    }

    Initialize-WebUISource
}

$actualCommit = (git -C $checkoutPath rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $upstreamCommit) {
    throw "Expected Open WebUI commit $upstreamCommit; found $actualCommit"
}

# Later patches can change context used by an earlier patch's reverse probe.
# Build the expected final index separately and compare normalized text,
# so a fully prepared checkout remains idempotent without reverting local work.
$tempIndex = Join-Path ([System.IO.Path]::GetTempPath()) ('nexrag-webui-index-' + [guid]::NewGuid())
$previousIndex = $env:GIT_INDEX_FILE
try {
    $env:GIT_INDEX_FILE = $tempIndex
    git -C $checkoutPath read-tree HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Could not create temporary WebUI verification index.' }
    foreach ($patch in $patches) {
        if (-not (Test-Path -LiteralPath $patch)) { throw "Required patch is missing: $patch" }
        git -C $checkoutPath apply --cached --ignore-whitespace --whitespace=nowarn $patch
        if ($LASTEXITCODE -ne 0) { throw "Patch chain is inconsistent: $patch" }
    }
    $changedFiles = @(git -C $checkoutPath diff --cached --name-only HEAD)
    $matches = $true
    foreach ($file in $changedFiles) {
        $fullPath = Join-Path $checkoutPath $file
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) { $matches = $false; break }
        $expected = (git -C $checkoutPath show ":$file") -join "`n"
        $actual = [System.IO.File]::ReadAllText($fullPath).Replace("`r`n", "`n").TrimEnd("`n")
        if ($LASTEXITCODE -ne 0 -or $actual -cne $expected.TrimEnd("`n")) { $matches = $false; break }
    }
    if ($matches) {
        Write-Host "Open WebUI is already prepared with all patches at $checkoutPath"
        exit 0
    }
} finally {
    $env:GIT_INDEX_FILE = $previousIndex
    if (Test-Path -LiteralPath $tempIndex) { Remove-Item -LiteralPath $tempIndex }
}

# Updated patch chains cannot be applied on top of a previous generated tree.
# Preserve that entire tree before rebuilding from the already verified pin;
# never reset or clean a checkout that may contain a maintainer's local work.
$localChanges = @(git -C $checkoutPath status --porcelain --untracked-files=normal)
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the generated WebUI checkout.' }
if ($localChanges.Count -gt 0) {
    $workspaceAbsolute = [System.IO.Path]::GetFullPath($projectRoot)
    $sourceAbsolute = [System.IO.Path]::GetFullPath($checkoutPath)
    $updateRoot = Join-Path $workspaceAbsolute ('.local-run/webui-updates/' + [guid]::NewGuid())
    $previousSource = Join-Path $updateRoot 'previous'
    $replacementSource = Join-Path $updateRoot 'repository'
    if ($sourceAbsolute -ne (Join-Path $workspaceAbsolute 'webui') `
        -or -not ([System.IO.Path]::GetFullPath($previousSource)).StartsWith((Join-Path $workspaceAbsolute '.local-run') + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase) `
        -or ((Get-Item -LiteralPath $sourceAbsolute -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw 'Refusing to rebuild a checkout outside the intended workspace.'
    }
    New-Item -ItemType Directory -Path $updateRoot | Out-Null
    git clone --local --no-checkout $checkoutPath $replacementSource
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the replacement WebUI checkout; original is unchanged.' }
    git -C $replacementSource checkout --detach $upstreamCommit
    if ($LASTEXITCODE -ne 0) { throw 'Could not check out the pinned replacement; original is unchanged.' }
    git -C $replacementSource remote set-url origin $upstreamUrl
    if ($LASTEXITCODE -ne 0) { throw 'Could not set replacement upstream; original is unchanged.' }
    [System.IO.Directory]::Move($sourceAbsolute, $previousSource)
    Move-VerifiedWebUISource $replacementSource
    # Reuse only the generated dependency cache; all source edits remain in
    # previous/. The existing dependency fingerprint is checked by the launcher.
    $oldModules = Join-Path $previousSource 'node_modules'
    $newModules = Join-Path $checkoutPath 'node_modules'
    if ((Test-Path -LiteralPath $oldModules -PathType Container) `
        -and -not ((Get-Item -LiteralPath $oldModules -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        [System.IO.Directory]::Move($oldModules, $newModules)
    }
    Write-Host "Preserved the previous generated source at $previousSource; preparing updated patches."
}

foreach ($patch in $patches) {
    if (-not (Test-Path -LiteralPath $patch)) {
        throw "Required patch is missing: $patch"
    }

    # PowerShell 5.1 treats Git's expected stderr on a failed probe as an
    # error record. Probe with Continue, then decide from Git's exit code.
    $ErrorActionPreference = 'Continue'
    git -C $checkoutPath apply --reverse --check --ignore-whitespace $patch 2>$null
    $alreadyApplied = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if ($alreadyApplied) {
        Write-Host "Already applied: $patch"
        continue
    }

    git -C $checkoutPath apply --check --ignore-whitespace $patch
    if ($LASTEXITCODE -ne 0) {
        throw "Patch does not apply cleanly; inspect the checkout: $patch"
    }

    git -C $checkoutPath apply --ignore-whitespace --whitespace=nowarn $patch
    if ($LASTEXITCODE -ne 0) {
        throw "Could not apply patch: $patch"
    }

    Write-Host "Applied: $patch"
}

Write-Host "Open WebUI is ready at $checkoutPath ($upstreamCommit)"
