# Run ONCE on a Windows machine with Python 3.11+ installed (this is the only
# place Python is needed -- target employee machines need nothing but PowerShell).
# Produces cyril-agent.exe and cyril-extension.zip directly in server\public\dist,
# so the next `wrangler deploy` ships them as static assets and install.ps1 can fetch them.
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."
$out = "$root\server\public\dist"
New-Item -ItemType Directory -Force -Path $out | Out-Null

pip install --quiet pyinstaller pywin32 mss pillow
pip install --quiet -r "$root\agent\requirements.txt"

pyinstaller --onefile --noconsole --name cyril-agent `
  --paths "$root\agent" `
  --distpath $out --workpath "$root\build" --specpath "$root\build" `
  "$root\agent\cyril_agent\__main__.py"

Compress-Archive -Path "$root\browser-extension\*" -DestinationPath "$out\cyril-extension.zip" -Force

$mb = [math]::Round((Get-Item "$out\cyril-agent.exe").Length / 1MB, 1)
Write-Host "Built:"
Write-Host "  $out\cyril-agent.exe ($mb MB)"
Write-Host "  $out\cyril-extension.zip"
if ($mb -ge 25) { Write-Warning "Cloudflare static assets cap files at 25 MiB. Host the .exe on R2 instead (see README)." }
Write-Host "Now run: cd server; npx wrangler deploy"
