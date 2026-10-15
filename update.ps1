<#
Update the whole Cyril server PC in one command (run it in the repo, on the PC that runs Docker):

  .\update.ps1                 # git pull, rebuild the Windows agent IF its code changed, rebuild + restart the server
  .\update.ps1 -ForceAgent     # rebuild the agent even if nothing in it changed
  .\update.ps1 -SkipPull       # use the code as it is (no git pull)
  .\update.ps1 -LocalBuild    # build the Windows agent here instead of downloading the CI build
  .\update.ps1 -DryRun         # say what it would do, change nothing
  .\update.ps1 -Version 0.3.0  # version label for the agent build (cosmetic; PCs update by build, not by version)

Why this is a script and not part of docker compose: the Windows agent (.exe) can only be built on Windows, and
Docker here runs Linux containers. The agent goes into server\dist, which the server serves directly, so a new
build reaches every PC without restarting anything. Windows and Mac agents are built by GitHub Actions (.github/workflows/agents.yml) and downloaded here.
#>
param(
    [switch]$SkipPull,
    [switch]$ForceAgent,
    [switch]$DryRun,
    [switch]$LocalBuild,
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$server = Join-Path $root "server"
$stampFile = Join-Path $root "build\agent-source.hash"

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Info($m) { Write-Host "    $m" }
function Run($label, [scriptblock]$cmd) {
    if ($DryRun) { Info "(dry run) would: $label"; return }
    Step $label
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "'$label' failed (exit code $LASTEXITCODE)" }
}

# 1. Code ---------------------------------------------------------------------------------------------------------
if ($SkipPull) { Info "Skipping git pull" }
else { Run "git pull origin main" { git -C $root pull origin main } }

# 2. Has anything the agent is built from changed? --------------------------------------------------------------------
# Hash of the agent's source, its dependency list, the browser extension and the build script. Server-only changes do
# not touch it, so they never trigger a (40 s) PyInstaller build.
function Get-AgentSourceHash {
    $files = foreach ($d in @("agent", "browser-extension")) {
        $p = Join-Path $root $d
        if (Test-Path $p) {
            Get-ChildItem -Path $p -Recurse -File | Where-Object {
                $_.FullName -notmatch '[\\/](__pycache__|tests|\.pytest_cache)[\\/]' -and $_.Extension -ne ".pyc" }
        }
    }
    $files += Get-Item (Join-Path $root "windows\build.ps1")
    $lines = $files | Sort-Object FullName | ForEach-Object {
        $rel = $_.FullName.Substring($root.Length)
        "$rel " + (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $sha = [Security.Cryptography.SHA256]::Create()
    ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace "-", "").ToLower()
}
$current = Get-AgentSourceHash
$previous = if (Test-Path $stampFile) { (Get-Content $stampFile -Raw).Trim() } else { "" }
$exePresent = Test-Path (Join-Path $server "dist\cyril.exe")
$build = $ForceAgent -or -not $exePresent -or ($current -ne $previous)

# 3. Agents ------------------------------------------------------------------------------------------------------------------
# Preferred: download what GitHub Actions built (Windows + both Mac CPUs) from the rolling "agent-latest" release.
# Needs the GitHub CLI (`gh`) signed in once. Fallback / -LocalBuild: build the Windows agent on this PC.
$fetched = $false
if (-not $LocalBuild -and -not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "    GitHub CLI (gh) not found, so the Mac agents cannot be downloaded: winget install --id GitHub.cli -e  (then gh auth login). Building the Windows agent locally." -ForegroundColor Yellow
}
if (-not $LocalBuild -and (Get-Command gh -ErrorAction SilentlyContinue)) {
    if ($DryRun) { Info "(dry run) would: gh release download agent-latest -> server\dist"; $fetched = $true }
    else {
        Step "download the CI-built agents (Windows + Mac)"
        $dist = Join-Path $server "dist"; New-Item -ItemType Directory -Force -Path $dist | Out-Null
        gh release download agent-latest --dir $dist --clobber --repo icestudios-d1/trakbot
        if ($LASTEXITCODE -eq 0) { $fetched = $true } else { Write-Host "    No CI build available yet (still running, or gh not signed in: gh auth login). Falling back to a local Windows build." -ForegroundColor Yellow }
    }
}
if (-not $fetched) {
    if ($build) {
        $why = if ($ForceAgent) { "forced" } elseif (-not $exePresent) { "no agent published yet" } else { "agent code changed" }
        $args2 = @(); if ($Version) { $args2 = @("-Version", $Version) }
        Run "build the Windows agent ($why)" { & (Join-Path $root "windows\build.ps1") @args2 }
        if (-not $DryRun) {
            New-Item -ItemType Directory -Force -Path (Split-Path $stampFile) | Out-Null
            Set-Content -Path $stampFile -Value $current -Encoding ASCII
        }
    } else {
        Info "Windows agent unchanged since the last build: keeping the published one (use -ForceAgent to rebuild)"
    }
}

# 4. Server -----------------------------------------------------------------------------------------------------------------
Push-Location $server
try { Run "docker compose up -d --build" { docker compose up -d --build } }
finally { Pop-Location }

# 5. Wait for it and say what is live ----------------------------------------------------------------------------------------
if (-not $DryRun) {
    Step "Waiting for the server"
    $up = $null
    foreach ($i in 1..30) {
        try { $up = Invoke-RestMethod -Uri "http://localhost:8000/api/health" -TimeoutSec 3; break } catch { Start-Sleep -Seconds 2 }
    }
    if ($up) { Info "Server is up, build $($up.build). Hard-refresh the dashboard (Ctrl+Shift+R) once." }
    else { Write-Host "    The server did not answer on http://localhost:8000 within a minute. Check: docker compose logs server --tail 30" -ForegroundColor Yellow }
}
Write-Host ""
Info "PCs pick up a new agent by themselves (daily ~03:00 and 5 minutes after every start)."
Info "To push it to one PC right now, on that PC (elevated): Start-ScheduledTask 'Cyril Updater'"
Info "Macs are built by GitHub Actions too (no Mac needed); ./mac/build.sh is only for building one by hand."
