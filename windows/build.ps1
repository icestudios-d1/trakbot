# Run ONCE on a Windows machine with Python 3.11+ installed (this is the only
# place Python is needed -- target employee machines need nothing but PowerShell).
# Produces cyril.exe, cyril-extension.zip and manifest.json, and places them
# where the server's CYRIL_DIST_DIR volume expects them so install.ps1 (first
# install) and the agent's own update check (later releases) can fetch them.
#
# Usage:
#   .\windows\build.ps1                  # first build
#   .\windows\build.ps1 -Version 0.2.1   # a release meant to replace a running 0.2.0 fleet
param(
    [string]$Version = "0.1.0",
    [string]$OutDir = $null  # defaults to the server's dist volume mountpoint
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."
$out = if ($OutDir) { $OutDir } else { "$root\server\dist" }
New-Item -ItemType Directory -Force -Path $out | Out-Null

pip install --quiet pyinstaller pywin32 mss pillow
pip install --quiet -r "$root\agent\requirements.txt"

pyinstaller --onefile --noconsole --name cyril `
  --paths "$root\agent" `
  --collect-all uiautomation --collect-all comtypes `
  --distpath $out --workpath "$root\build" --specpath "$root\build" `
  "$root\agent\cyril_entry.py"

# Never publish an exe that cannot start (a relative-import crash once shipped this way). --selftest exits 0
# as soon as the program has started; a crash shows PyInstaller's error dialog and a non-zero exit code.
$test = Start-Process -FilePath "$out\cyril.exe" -ArgumentList "--selftest" -Wait -PassThru
if ($test.ExitCode -ne 0) {
    Remove-Item "$out\cyril.exe", "$out\manifest.json" -Force -ErrorAction SilentlyContinue
    throw "The built cyril.exe failed its self-test (exit code $($test.ExitCode)). Nothing was published."
}
Write-Host "Self-test passed."

Compress-Archive -Path "$root\browser-extension\*" -DestinationPath "$out\cyril-extension.zip" -Force

$hash = (Get-FileHash "$out\cyril.exe" -Algorithm SHA256).Hash.ToLower()
$build = (git -C $root rev-parse --short HEAD 2>$null)
$manifest = @{
    version      = $Version
    build        = "$build"
    selftest     = $true   # tells install.ps1 this exe understands --selftest
    windows_exe  = @{ url = "/dist/cyril.exe"; sha256 = $hash }
} | ConvertTo-Json -Depth 5
# UTF-8 without BOM (Windows PowerShell 5.1's "-Encoding UTF8" adds one, which breaks JSON readers).
[IO.File]::WriteAllText("$out\manifest.json", $manifest, (New-Object Text.UTF8Encoding $false))

$mb = [math]::Round((Get-Item "$out\cyril.exe").Length / 1MB, 1)
Write-Host "Built (version $Version):"
Write-Host "  $out\cyril.exe ($mb MB), sha256 $hash"
Write-Host "  $out\cyril-extension.zip"
Write-Host "  $out\manifest.json"
Write-Host ""
Write-Host "Published: these are already in server\dist, which the server serves directly (no copy, no restart)."
Write-Host "  Only if you built on a different machine than the server: copy the three files into the server's server\dist folder."
Write-Host "New PCs: use the one-liner on the dashboard's 'Install agents' page."
Write-Host "Existing PCs: their 'Cyril Updater' task re-runs the installer daily (~03:00) and swaps in the new build; to do it now, run the one-liner again on that PC."
