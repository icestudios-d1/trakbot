# Run ONCE on a Windows machine with Python 3.11+ installed (this is the only
# place Python is needed -- target employee machines need nothing but PowerShell).
# Produces dist\trakbot-agent.exe and dist\trakbot-extension.zip, which you then
# upload to the TrakBot server's dist/ folder (TRAKBOT_DIST_DIR) so install.ps1
# can fetch them.
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."

pip install --quiet pyinstaller pywin32 mss pillow
pip install --quiet -r "$root\agent\requirements.txt"

pyinstaller --onefile --noconsole --name trakbot-agent `
  --paths "$root\agent" `
  --distpath "$root\dist" --workpath "$root\build" --specpath "$root\build" `
  "$root\agent\trakbot_agent\__main__.py"

Compress-Archive -Path "$root\browser-extension\*" -DestinationPath "$root\dist\trakbot-extension.zip" -Force

Write-Host "Built:"
Write-Host "  $root\dist\trakbot-agent.exe"
Write-Host "  $root\dist\trakbot-extension.zip"
Write-Host "Copy both into the TrakBot server's dist/ folder (TRAKBOT_DIST_DIR), then run install.ps1 on target machines."
