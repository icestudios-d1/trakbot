<#
Cyril agent installer. Needs only PowerShell on the target machine (no git, no npm,
no Python). Safe to run again: it updates an existing install in place.

The easiest way is the one-liner on your server's "Install" page in the dashboard.
It looks like this (run in an ELEVATED PowerShell; the server fills in its own
address, and the agent key travels in the URL):

  & ([scriptblock]::Create((irm 'https://cyril.example.com/install.ps1?key=AGENT_KEY')))

Optional switches:
  -User "Priya"            report under this name instead of the Windows login
  -SkipBrowserPolicy       do not disable Incognito/InPrivate via browser policy
Remove it again with the uninstall one-liner on the same page.
#>
param(
    [string]$ServerUrl = '{{SERVER_URL}}',
    [string]$AgentKey = '{{AGENT_KEY}}',
    [string]$User = $null,
    [switch]$SkipBrowserPolicy
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is ~10x faster without the progress bar
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

function Step($msg) { Write-Host "  [..] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [ok] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!!] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host ""; Write-Host "  [xx] $msg" -ForegroundColor Red; Write-Host ""; throw "Cyril install failed: $msg" }

Write-Host ""
Write-Host "Cyril agent installer" -ForegroundColor White

# 1. Preconditions -------------------------------------------------------------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run this in an elevated PowerShell (right-click PowerShell, 'Run as administrator')."
}
$ServerUrl = "$ServerUrl".Trim().TrimEnd("/")
if (-not $ServerUrl) { Fail "No server address. Use the command from the dashboard's Install page." }
if (-not "$AgentKey".Trim()) { Fail "No agent key. Use the command from the dashboard's Install page (it includes ?key=...)." }

$installDir = Join-Path $env:ProgramFiles "Cyril"
$cfgDir     = Join-Path $env:ProgramData "Cyril"
$exe        = Join-Path $installDir "cyril.exe"
$cfgFile    = Join-Path $cfgDir "config.json"
$taskName   = "Cyril Agent"
$regKey     = "HKLM:\SOFTWARE\Cyril"
$updating   = Test-Path $exe

# 2. Check the server and that the agent build is published ---------------------
Step "Contacting $ServerUrl"
try { Invoke-WebRequest -UseBasicParsing -Uri "$ServerUrl/api/health" -TimeoutSec 15 | Out-Null }
catch { Fail "Cannot reach $ServerUrl ($($_.Exception.Message)). Check the address and this machine's network." }
Ok "Server reachable"

$expectedHash = $null
try { $expectedHash = (Invoke-RestMethod -UseBasicParsing -Uri "$ServerUrl/api/agent/version" -TimeoutSec 15).windows_exe.sha256 } catch { }

# 3. Download first. A running agent is only stopped once the new file is safely in hand,
#    so a failed update never leaves a machine unmonitored. ----------------------------
if ($updating) { Step "Existing install found, updating it" }
New-Item -ItemType Directory -Force -Path $installDir, $cfgDir | Out-Null

Step "Downloading the agent"
$tmp = Join-Path $installDir "cyril.exe.download"
try { Invoke-WebRequest -UseBasicParsing -Uri "$ServerUrl/dist/cyril.exe" -OutFile $tmp -TimeoutSec 300 }
catch {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    Fail "Could not download $ServerUrl/dist/cyril.exe ($($_.Exception.Message)). The agent build has not been published to the server yet - run windows\build.ps1 on a build machine and copy its output into the server's dist folder."
}
if ($expectedHash) {
    $actual = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expectedHash.ToLower()) { Remove-Item $tmp -Force; Fail "Downloaded file does not match the published checksum. Nothing was installed." }
    Ok "Checksum verified"
}
if ($updating) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Get-Process -Name "cyril" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
}
Move-Item -Force $tmp $exe
Ok "Agent installed to $installDir"

try {
    $zip = Join-Path $installDir "cyril-extension.zip"
    Invoke-WebRequest -UseBasicParsing -Uri "$ServerUrl/dist/cyril-extension.zip" -OutFile $zip -TimeoutSec 60
    Expand-Archive -Force $zip (Join-Path $installDir "extension")
    Ok "Browser extension unpacked to $installDir\extension"
} catch { Warn "Browser extension step skipped: $($_.Exception.Message)" }

# 5. Config. Holds the shared agent key, so only SYSTEM/Administrators may read it ---------
$cfg = @{ server = $ServerUrl; key = $AgentKey.Trim() }
if ($User) { $cfg.user = $User }
elseif (Test-Path $cfgFile) {
    try { $old = Get-Content $cfgFile -Raw | ConvertFrom-Json; if ($old.user) { $cfg.user = $old.user } } catch { }
}
# UTF-8 *without* BOM (Windows PowerShell 5.1's "-Encoding UTF8" adds one).
[IO.File]::WriteAllText($cfgFile, ($cfg | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
icacls $cfgDir /inheritance:r | Out-Null
icacls $cfgDir /grant:r "SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)F" | Out-Null
Ok "Config written (admin-only)"

# 6. Start at boot as SYSTEM, restart if it dies --------------------------------------------
# SYSTEM (not the logged-in user) so a standard employee cannot stop or remove it.
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
$action    = New-ScheduledTaskAction -Execute $exe -Argument "--config `"$cfgFile`""
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Ok "Scheduled task '$taskName' registered and started"

# 7. Browser policy: disable Incognito/InPrivate (it hides URLs from the agent) --------------------
# Only values we create are recorded, so uninstall can remove exactly those and nothing else.
if ($SkipBrowserPolicy) {
    Warn "Skipping browser policy (-SkipBrowserPolicy)"
} else {
    New-Item -Path $regKey -Force | Out-Null
    # Keep what an earlier run recorded: on a re-run the values already exist, but they are still ours to remove.
    $set = @()
    $prev = Get-ItemProperty -Path $regKey -Name "IncognitoPolicySetFor" -ErrorAction SilentlyContinue
    if ($prev -and $prev.IncognitoPolicySetFor) { $set = @("$($prev.IncognitoPolicySetFor)" -split ";" | Where-Object { $_ }) }
    foreach ($browserKey in @("Google\Chrome", "Microsoft\Edge")) {
        $p = "HKLM:\SOFTWARE\Policies\$browserKey"
        $existing = Get-ItemProperty -Path $p -Name "IncognitoModeAvailability" -ErrorAction SilentlyContinue
        if ($null -eq $existing) {
            New-Item -Path $p -Force | Out-Null
            Set-ItemProperty -Path $p -Name "IncognitoModeAvailability" -Value 1   # 1 = disabled
            $set += $browserKey
        }
    }
    $set = @($set | Select-Object -Unique)
    Set-ItemProperty -Path $regKey -Name "IncognitoPolicySetFor" -Value ($set -join ";")
    if ($set.Count) { Ok "Incognito/InPrivate disabled in: $($set -join ', ')" } else { Ok "Browser policy already configured, left as is" }
}

# 8. Confirm it is really running ----------------------------------------------------------------
$running = $false
foreach ($i in 1..10) { if (Get-Process -Name "cyril" -ErrorAction SilentlyContinue) { $running = $true; break }; Start-Sleep -Seconds 1 }
Write-Host ""
if ($running) { Write-Host "Cyril agent is running." -ForegroundColor Green }
else { Warn "The agent process was not seen yet. Check: Get-ScheduledTask 'Cyril Agent' | Get-ScheduledTaskInfo" }
Write-Host "This machine should appear on $ServerUrl/install within a minute or two."
Write-Host "To remove it:  & ([scriptblock]::Create((irm '$ServerUrl/uninstall.ps1')))"
Write-Host ""
