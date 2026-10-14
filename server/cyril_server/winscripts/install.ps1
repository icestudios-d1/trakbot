<#
Cyril agent installer. Needs only PowerShell on the target machine (no git, no npm,
no Python). Safe to run again: it updates an existing install in place.

The easiest way is the one-liner on your server's "Install agents" page in the dashboard.
It looks like this (run in an ELEVATED PowerShell; the server fills in its own
address, and the agent key travels in the URL):

  & ([scriptblock]::Create((irm 'https://cyril.example.com/install.ps1?key=AGENT_KEY')))

Optional switches:
  -User "Priya"     report under this name instead of the Windows login
  -Unattended       no questions, no first-report wait (used by the daily updater task)
Remove it again with the uninstall one-liner on the same page.

How it runs: the agent has to live inside the logged-in user's desktop session (a service or
boot-time SYSTEM task has no desktop: no windows to see, no screen to capture). So it starts
when any user logs on, and a task re-launches it every 5 minutes if it was closed. A second
task, running as SYSTEM, re-runs this installer daily so new builds reach every PC.
#>
param(
    [string]$ServerUrl = '{{SERVER_URL}}',
    [string]$AgentKey = '{{AGENT_KEY}}',
    [string]$User = $null,
    [switch]$Unattended
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is ~10x faster without the progress bar
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

function Step($msg) { if (-not $Unattended) { Write-Host "  [..] $msg" -ForegroundColor Cyan } }
function Ok($msg)   { if (-not $Unattended) { Write-Host "  [ok] $msg" -ForegroundColor Green } }
function Warn($msg) { Write-Host "  [!!] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host ""; Write-Host "  [xx] $msg" -ForegroundColor Red; Write-Host ""; throw "Cyril install failed: $msg" }

if (-not $Unattended) { Write-Host ""; Write-Host "Cyril agent installer" -ForegroundColor White }

# 1. Preconditions -------------------------------------------------------------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run this in an elevated PowerShell (right-click PowerShell, 'Run as administrator')."
}
$ServerUrl = "$ServerUrl".Trim().TrimEnd("/")
if (-not $ServerUrl) { Fail "No server address. Use the command from the dashboard's Install page." }
if (-not "$AgentKey".Trim()) { Fail "No agent key. Use the command from the dashboard's Install page (it includes ?key=...)." }
$AgentKey = $AgentKey.Trim()

$installDir = Join-Path $env:ProgramFiles "Cyril"
$cfgDir     = Join-Path $env:ProgramData "Cyril"
$dataDir    = Join-Path $cfgDir "data"          # log + spool, writable by the logged-in user
$exe        = Join-Path $installDir "cyril.exe"
$cfgFile    = Join-Path $cfgDir "config.json"
$taskName   = "Cyril Agent"
$updaterName = "Cyril Updater"
$regKey     = "HKLM:\SOFTWARE\Cyril"
$usersSid   = "S-1-5-32-545"                    # BUILTIN\Users, locale-independent
$updating   = Test-Path $exe

# 2. Check the server and that the agent build is published ---------------------
Step "Contacting $ServerUrl"
try { Invoke-WebRequest -UseBasicParsing -Uri "$ServerUrl/api/health" -TimeoutSec 15 | Out-Null }
catch { Fail "Cannot reach $ServerUrl ($($_.Exception.Message)). Check the address and this machine's network." }
Ok "Server reachable"

$expectedHash = $null
try { $expectedHash = (Invoke-RestMethod -UseBasicParsing -Uri "$ServerUrl/api/agent/version" -TimeoutSec 15).windows_exe.sha256 } catch { }
$upToDate = $false
if ($updating -and $expectedHash) { $upToDate = ((Get-FileHash $exe -Algorithm SHA256).Hash.ToLower() -eq $expectedHash.ToLower()) }

# 3. Download first. A running agent is only stopped once the new file is safely in hand,
#    so a failed update never leaves a machine unmonitored. ----------------------------
New-Item -ItemType Directory -Force -Path $installDir, $cfgDir, $dataDir | Out-Null
if ($upToDate) {
    Ok "Agent is already the published version"
} else {
    if ($updating) { Step "Existing install found, updating it" }
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
}

# 4. Config ----------------------------------------------------------------------------------------------
# The agent runs as the logged-in user, so it must be able to READ its config (server + key).
# Only admins and SYSTEM can change it. The data folder (log + spool) is the one place users can write.
$cfg = @{ server = $ServerUrl; key = $AgentKey }
if ($User) { $cfg.user = $User }
elseif (Test-Path $cfgFile) {
    try { $old = Get-Content $cfgFile -Raw | ConvertFrom-Json; if ($old.user) { $cfg.user = $old.user } } catch { }
}
# UTF-8 *without* BOM (Windows PowerShell 5.1's "-Encoding UTF8" adds one).
[IO.File]::WriteAllText($cfgFile, ($cfg | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
icacls $cfgDir /inheritance:r | Out-Null
icacls $cfgDir /grant:r "SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)F" "*${usersSid}:(OI)(CI)RX" | Out-Null
icacls $dataDir /grant:r "*${usersSid}:(OI)(CI)M" | Out-Null
Ok "Config written"

# 5. Tasks ---------------------------------------------------------------------------------------------------
# Old versions ran the agent as SYSTEM at boot. SYSTEM has no desktop, so any such copy is useless: stop it.
Get-Process -Name "cyril" -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq 0 } | Stop-Process -Force -ErrorAction SilentlyContinue

# 5a. The agent: at every logon, in that user's session, plus a 5-minute re-launch if it was closed.
$usersName = ([Security.Principal.SecurityIdentifier]$usersSid).Translate([Security.Principal.NTAccount]).Value
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
$action    = New-ScheduledTaskAction -Execute $exe -Argument "--config `"$cfgFile`" --update-check-every 0"
$triggers  = @(
    (New-ScheduledTaskTrigger -AtLogOn),
    (New-ScheduledTaskTrigger -Once -At ((Get-Date).Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650))
)
$principal = New-ScheduledTaskPrincipal -GroupId $usersName -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings -Force | Out-Null
Ok "Task '$taskName' registered (runs in the logged-in user's session)"

# 5b. The updater: SYSTEM, daily. Re-runs this installer, which does nothing unless a new build is published.
Unregister-ScheduledTask -TaskName $updaterName -Confirm:$false -ErrorAction SilentlyContinue
$cmd = '$c = Get-Content ''' + $cfgFile + ''' -Raw | ConvertFrom-Json; & ([scriptblock]::Create((irm ($c.server + ''/install.ps1?key='' + [uri]::EscapeDataString($c.key))))) -Unattended'
$uAction    = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$cmd`""
$uTrigger   = New-ScheduledTaskTrigger -Daily -At "03:00" -RandomDelay (New-TimeSpan -Minutes 30)
$uPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$uSettings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $updaterName -Action $uAction -Trigger $uTrigger -Principal $uPrincipal -Settings $uSettings -Force | Out-Null
Ok "Task '$updaterName' registered (daily, picks up new builds)"

# 6. Earlier versions disabled Incognito/InPrivate by browser policy. Cyril does not do that any more
#    (private windows are flagged and screenshotted instead), so undo it where this installer had set it.
if (Test-Path $regKey) {
    $set = (Get-ItemProperty -Path $regKey -Name "IncognitoPolicySetFor" -ErrorAction SilentlyContinue).IncognitoPolicySetFor
    foreach ($b in @("$set" -split ";" | Where-Object { $_ })) {
        try { Remove-ItemProperty -Path "HKLM:\SOFTWARE\Policies\$b" -Name "IncognitoModeAvailability" -ErrorAction Stop; Ok "Incognito/InPrivate re-enabled in $b (an earlier install had disabled it)" } catch { }
    }
    Remove-Item -Recurse -Force $regKey -ErrorAction SilentlyContinue
}

# 7. Start it now, then confirm the server really receives reports -------------------------------------
$since = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - 5
if (-not (Get-Process -Name "cyril" -ErrorAction SilentlyContinue)) { Start-ScheduledTask -TaskName $taskName }
if ($Unattended) { return }

Step "Waiting for the first report (up to ~2 minutes)"
$device = $null
try { $device = [Microsoft.Win32.RegistryKey]::OpenBaseKey('LocalMachine', 'Registry64').OpenSubKey('SOFTWARE\Microsoft\Cryptography').GetValue('MachineGuid') } catch { }
$seen = $null
foreach ($i in 1..24) {
    Start-Sleep -Seconds 5
    try { $seen = Invoke-RestMethod -UseBasicParsing -Headers @{ "X-Api-Key" = $AgentKey } -TimeoutSec 15 -Uri "$ServerUrl/api/agent/seen?device=$([uri]::EscapeDataString("$device"))&since=$since" } catch { $seen = $null }
    if ($seen -and $seen.seen) { break }
}

function Show-Diagnostics {
    Write-Host ""
    Write-Host "  Diagnostics:" -ForegroundColor White
    $procs = Get-Process -Name "cyril" -ErrorAction SilentlyContinue
    if ($procs) { $procs | ForEach-Object { Write-Host "    cyril.exe pid $($_.Id) running in session $($_.SessionId)  (session 0 = background services, cannot see any desktop)" } }
    else { Write-Host "    cyril.exe is NOT running" }
    $info = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
    if ($info) { Write-Host "    task last run: $($info.LastRunTime), result: 0x$('{0:X}' -f $info.LastTaskResult)" }
    $log = Join-Path $dataDir "agent.log"
    if (Test-Path $log) { Write-Host "    last lines of $log :"; Get-Content $log -Tail 12 | ForEach-Object { Write-Host "      $_" } }
    else { Write-Host "    no agent log yet at $log (the agent never started)" }
}

Write-Host ""
if ($seen -and $seen.seen -and $seen.with_app -gt 0) {
    Write-Host "Cyril is installed and reporting: $($seen.events) report(s) received, $($seen.with_app) with app names." -ForegroundColor Green
} elseif ($seen -and $seen.seen) {
    Warn "The server is receiving reports, but none contain an app name, so the agent cannot see the desktop."
    Show-Diagnostics
} else {
    Warn "Installed, but the server has not received a report from this machine yet."
    Show-Diagnostics
}
Write-Host ""
Write-Host "Dashboard: $ServerUrl/install"
Write-Host "To remove it:  & ([scriptblock]::Create((irm '$ServerUrl/uninstall.ps1')))"
Write-Host ""
