<#
NetMon agent installer (formerly "Cyril"). Needs only PowerShell on the target machine (no git, no npm,
no Python). Safe to run again: it updates an existing install in place.

A PC that still has the old Cyril agent is moved over by this same script, with no step for the user (the old
"Cyril Updater" task fetches this file like any other update). The hand-over is: download and self-test the new
program FIRST; only then stop the Cyril agent, copy its config and unsent data into the NetMon folder, start
NetMon and check that it is running. If anything fails, the Cyril agent is switched back on and nothing is lost.
The old Cyril folders and tasks are first only switched off. They are removed on a LATER run (the one after the
hand-over, so it never happens inside a Cyril task), and only once NetMon has proved itself: its own health.json,
which Cyril never wrote, must be recent and show uploads working. Until then Cyril's files stay as the fallback.

The easiest way is the one-liner on your server's "Install agents" page in the dashboard.
It looks like this (run in an ELEVATED PowerShell; the server fills in its own
address, and the agent key travels in the URL):

  & ([scriptblock]::Create((irm 'https://your-server.example.com/install.ps1?key=AGENT_KEY')))

Optional switches:
  -User "Priya"     report under this name instead of the Windows login
  -Silent           quiet install for roll-outs (GPO, Intune, RMM, a USB stick): no step list, no first-report
                    wait, one line at the end. Everything is still installed and started before it returns;
                    a failure still stops with an error (non-zero exit code when run with powershell -Command).
  -Unattended       the same, with no final line at all (used by the daily updater task)
Remove it again with the uninstall one-liner on the same page.

How it runs: the agent has to live inside the logged-in user's desktop session (a service or
boot-time SYSTEM task has no desktop: no windows to see, no screen to capture). So it starts
when any user logs on, and a task re-launches it every 5 minutes if it was closed. A second
task, running as SYSTEM, re-runs this installer daily so new builds reach every PC.
#>
param(
    [string]$ServerUrl = '{{SERVER_URL}}',
    [string]$AgentKey = '{{AGENT_KEY}}',
    [string]$PayloadKey = '{{PAYLOAD_KEY}}',   # the server's PUBLIC key: the agent seals everything to it and cannot read it back
    [string]$User = $null,
    [switch]$Unattended,
    [switch]$Silent
)
if ($Silent) { $Unattended = $true }
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is ~10x faster without the progress bar
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

function Step($msg) { if (-not $Unattended) { Write-Host "  [..] $msg" -ForegroundColor Cyan } }
function Ok($msg)   { if (-not $Unattended) { Write-Host "  [ok] $msg" -ForegroundColor Green } }
function Warn($msg) { Write-Host "  [!!] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host ""; Write-Host "  [xx] $msg" -ForegroundColor Red; Write-Host ""; throw "NetMon install failed: $msg" }

if (-not $Unattended) { Write-Host ""; Write-Host "Welcome to NetMon by ICE" -ForegroundColor White }

# 1. Preconditions -------------------------------------------------------------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run this in an elevated PowerShell (right-click PowerShell, 'Run as administrator')."
}
$ServerUrl = "$ServerUrl".Trim().TrimEnd("/")
if (-not $ServerUrl) { Fail "No server address. Use the command from the dashboard's Install page." }
if (-not "$AgentKey".Trim()) { Fail "No agent key. Use the command from the dashboard's Install page (it includes ?key=...)." }
$AgentKey = $AgentKey.Trim()

$installDir = Join-Path $env:ProgramFiles "NetMon"
$cfgDir     = Join-Path $env:ProgramData "NetMon"
$dataDir    = Join-Path $cfgDir "data"          # log + spool, writable by the logged-in user
$exe        = Join-Path $installDir "netmon.exe"
$procName   = "netmon"
$cfgFile    = Join-Path $cfgDir "config.json"
$taskName   = "NetMon Agent"
# The updater and recovery-check tasks are named AWAY from the product on purpose: an employee who searches for and
# deletes "cyril"/"netmon"-named things (fonts, old folders, whatever) must not be able to take out the one thing
# that would otherwise bring the agent straight back. Picked once; a later rebrand should not have to repeat this.
$updaterName  = "GR5523 Port"
$recoveryName = "GR5523 Port Monitor"
# every name this task has carried before this one: cleaned up on sight, every run, same as the old Cyril names
$legacyUpdaterNames = @("ICEStudios IT Services", "ICEStudios IT Monitor", "NetMon Updater", "NetMon Recovery Check")
$migratedMarker = Join-Path $cfgDir "migrated-from-cyril.txt"   # written once the hand-over from Cyril has succeeded
# What the old product left on a PC. Only ever switched off here (see the header), never deleted.
$oldInstallDir = Join-Path $env:ProgramFiles "Cyril"
$oldCfgDir     = Join-Path $env:ProgramData "Cyril"
$oldCfgFile    = Join-Path $oldCfgDir "config.json"
$oldDataDir    = Join-Path $oldCfgDir "data"
$oldExe        = Join-Path $oldInstallDir "cyril.exe"
$oldProc       = "cyril"
$oldAgentTask  = "Cyril Agent"
$oldUpdaterTasks = @("Cyril Updater", "Cyril Recovery Check")
$regKey     = "HKLM:\SOFTWARE\Cyril"       # the old product's key: an earlier version stored its Incognito undo here (section 6)
$usersSid   = "S-1-5-32-545"                    # BUILTIN\Users, locale-independent
$updating   = Test-Path $exe

# Is the old Cyril agent still on this PC and not yet handed over?
$oldPresent = (Test-Path $oldCfgDir) -or (Test-Path $oldExe) -or [bool](Get-ScheduledTask -TaskName $oldAgentTask -ErrorAction SilentlyContinue)
$migrating  = $oldPresent -and -not (Test-Path $migratedMarker)
# Already handed over on an earlier run, and Cyril's leftovers are still there: they can be removed (see 5e). Decided
# here, before this run can write the marker, so the run that does the hand-over never also does the clean-up.
$cleanupOld = $oldPresent -and (Test-Path $migratedMarker)

function Quiet([scriptblock]$b) { try { & $b } catch { } }
function Get-DeviceId {
    try { return [Microsoft.Win32.RegistryKey]::OpenBaseKey('LocalMachine', 'Registry64').OpenSubKey('SOFTWARE\Microsoft\Cryptography').GetValue('MachineGuid') } catch { return "" }
}
function PSQ([string]$s) { "'" + $s.Replace("'", "''") + "'" }   # a value as a single-quoted PowerShell literal

# 2. Check the server and that the agent build is published ---------------------
Step "Contacting $ServerUrl"
try { Invoke-WebRequest -UseBasicParsing -Uri "$ServerUrl/api/health" -TimeoutSec 15 | Out-Null }
catch { Fail "Cannot reach $ServerUrl ($($_.Exception.Message)). Check the address and this machine's network." }
Ok "Server reachable"

# Remote uninstall backstop: when run by the daily/boot updater task (which passes -Unattended), honour a removal
# order from the server -- the recovery task handles it within 15 minutes, this catches a machine that was off then.
# Never during an interactive admin install, so re-running the installer by hand cannot trip it.
if ($Unattended) {
    $rmDev = Get-DeviceId
    try { $rm = Invoke-RestMethod -UseBasicParsing -Headers @{ "X-Api-Key" = $AgentKey } -TimeoutSec 20 -Uri "$ServerUrl/api/agent/removal?device=$([uri]::EscapeDataString("$rmDev"))" } catch { $rm = "" }
    if ("$rm" -match "remove=1") {
        & ([scriptblock]::Create((Invoke-RestMethod "$ServerUrl/uninstall.ps1")))
        try {
            $body = (@{ device = "$rmDev"; state = "removed"; detail = "removed by updater task" } | ConvertTo-Json -Compress)
            Invoke-RestMethod -UseBasicParsing -Method Post -Headers @{ "X-Api-Key" = $AgentKey; "Content-Type" = "application/json" } -TimeoutSec 20 -Uri "$ServerUrl/api/agent/removal-state" -Body $body | Out-Null
        } catch { }
        return
    }
}

$expectedHash = $null; $buildInfo = ""; $canSelftest = $false; $ver = $null
try {
    $ver = Invoke-RestMethod -UseBasicParsing -Uri "$ServerUrl/api/agent/version" -TimeoutSec 15
    $expectedHash = $ver.windows_exe.sha256
    $canSelftest = ($ver.selftest -eq $true)
    if ($ver.version) { $buildInfo = "version $($ver.version)" + $(if ($ver.build) { ", build $($ver.build)" } else { "" }) }
} catch { }
if (-not $expectedHash) { Warn "The server publishes no checksum for the agent (dist\manifest.json missing?), so the download cannot be verified. Rebuild with windows\build.ps1." }
$upToDate = $false
if ($updating -and $expectedHash) { $upToDate = ((Get-FileHash $exe -Algorithm SHA256).Hash.ToLower() -eq $expectedHash.ToLower()) }

# 3. Download first. A running agent is only stopped once the new file is safely in hand,
#    so a failed update never leaves a machine unmonitored. ----------------------------
New-Item -ItemType Directory -Force -Path $installDir, $cfgDir, $dataDir | Out-Null
if ($upToDate) {
    Ok "Agent is already the published build ($buildInfo). If it is crashing, rebuild and publish a new one with windows\build.ps1."
} else {
    if ($updating) { Step "Existing install found, updating it" }
    Step "Downloading"
    $tmp = Join-Path $installDir "netmon.new.exe"
    # ?h=<hash>: a different URL for every build, so no proxy/CDN can hand back an old cached exe.
    $bust = if ($expectedHash) { $expectedHash.Substring(0, 16) } else { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
    try { Invoke-WebRequest -UseBasicParsing -Uri "$ServerUrl/dist/netmon.exe?h=$bust" -OutFile $tmp -TimeoutSec 300 }
    catch {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        Fail "Could not download $ServerUrl/dist/netmon.exe ($($_.Exception.Message)). The agent build has not been published to the server yet - run windows\build.ps1 in the repo on the server machine (it writes into server\dist, which the server serves; if you built elsewhere, copy the files there)."
    }
    if ($expectedHash) {
        $actual = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expectedHash.ToLower()) { Remove-Item $tmp -Force; Fail "Downloaded file does not match the published checksum. Nothing was installed." }
        Ok "Checksum verified"
    }
    if ($canSelftest) {
        # Run the downloaded program once before it replaces anything: a broken build is rejected here,
        # on this PC, instead of installing and silently never reporting.
        $t = Start-Process -FilePath $tmp -ArgumentList "--selftest" -Wait -PassThru
        if ($t.ExitCode -ne 0) { Remove-Item $tmp -Force; Fail "The downloaded netmon.exe does not start (self-test exit code $($t.ExitCode)). It is a broken build: rebuild with windows\build.ps1 and run this again. Nothing was installed." }
        Ok "Start Successful"
    }
    if ($updating) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Get-Process -Name $procName -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
    Move-Item -Force $tmp $exe
    Ok "System Installed to $installDir$(if ($buildInfo) { " ($buildInfo)" })"
}

# 4. Config ----------------------------------------------------------------------------------------------
# The agent runs as the logged-in user, so it must be able to READ its config (server + key).
# Only admins and SYSTEM can change it. The data folder (log + spool) is the one place users can write.
$cfg = @{ server = $ServerUrl; key = $AgentKey }
if ("$PayloadKey".Trim() -and "$PayloadKey" -notmatch '[{]') { $cfg.payload_key = "$PayloadKey".Trim() }
if ($User) { $cfg.user = $User }
else {
    foreach ($f in @($cfgFile, $oldCfgFile)) {   # this install's own config first, else the one the Cyril agent had
        if (Test-Path $f) {
            try { $old = Get-Content $f -Raw | ConvertFrom-Json; if ($old.user) { $cfg.user = $old.user; break } } catch { }
        }
    }
}
# UTF-8 *without* BOM (Windows PowerShell 5.1's "-Encoding UTF8" adds one).
[IO.File]::WriteAllText($cfgFile, ($cfg | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
icacls $cfgDir /inheritance:r | Out-Null
icacls $cfgDir /grant:r "SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)F" "*${usersSid}:(OI)(CI)RX" | Out-Null
icacls $dataDir /grant:r "*${usersSid}:(OI)(CI)M" | Out-Null
# Stamp which build is installed here; the agent reports it so the dashboard can show "update pending".
$stamp = if ($ver -and $ver.build) { "$($ver.build)" } elseif ($expectedHash) { $expectedHash.Substring(0, 12) } else { "" }
if ($stamp) { [IO.File]::WriteAllText((Join-Path $cfgDir "build.txt"), $stamp) }
Ok "Config written"

# 5. Tasks ---------------------------------------------------------------------------------------------------
# Old versions ran the agent as SYSTEM at boot. SYSTEM has no desktop, so any such copy is useless: stop it.
Get-Process -Name $procName -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq 0 } | Stop-Process -Force -ErrorAction SilentlyContinue

function Send-MigrationReport([bool]$ok, [string]$detail, [string]$from = "cyril", [string]$to = "") {
    # Visible in the dashboard's update log, so the roll-out can be checked machine by machine. Never blocks the install.
    try {
        $who = if ($cfg.user) { "$($cfg.user)" } elseif ($env:USERNAME) { $env:USERNAME } else { $env:COMPUTERNAME }
        $dev = Get-DeviceId; if (-not $dev) { $dev = $env:COMPUTERNAME }
        $body = @{ user = $who; device = "$dev"; from_version = $from; to_version = $(if ($to) { $to } else { "netmon $($ver.version)" }); ok = $ok; detail = $detail.Substring(0, [Math]::Min(1900, $detail.Length)) } | ConvertTo-Json
        Invoke-RestMethod -UseBasicParsing -Method Post -Uri "$ServerUrl/api/agent/update-log" -Headers @{ "X-Api-Key" = $AgentKey } `
            -ContentType "application/json" -Body $body -TimeoutSec 15 | Out-Null
    } catch { }
}

function Register-AgentTask {
    # The agent: at every logon, in that user's session, plus a 5-minute re-launch if it was closed.
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
}

# 5a. Hand over from Cyril. Everything above (download, checksum, self-test, config) has already succeeded, so the
#     old agent is only stopped now. It is switched off (task disabled, process stopped), never deleted: if NetMon
#     does not come up, it is switched straight back on. The old UPDATER and recovery tasks are left enabled until
#     NetMon is confirmed running (they are the retry path), and may be the very process running this script, so
#     they are never stopped here, only disabled afterwards (5d).
function Restore-Cyril([string]$why) {
    Warn "NetMon could not take over ($why). Switching the Cyril agent back on; the next update will try again."
    Quiet { Stop-ScheduledTask -TaskName $taskName }
    Quiet { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Get-Process -Name $procName -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Quiet { Enable-ScheduledTask -TaskName $oldAgentTask | Out-Null }
    Quiet { Start-ScheduledTask -TaskName $oldAgentTask }
    Send-MigrationReport $false "rolled back to Cyril: $why"
}

if ($migrating) {
    Step "Moving this PC from Cyril to NetMon"
    $copied = 0
    try {
        Quiet { Stop-ScheduledTask -TaskName $oldAgentTask -ErrorAction Stop }
        Quiet { Disable-ScheduledTask -TaskName $oldAgentTask -ErrorAction Stop | Out-Null }
        Get-Process -Name $oldProc -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        if (Get-Process -Name $oldProc -ErrorAction SilentlyContinue) { throw "the Cyril agent would not stop" }
        Ok "Cyril agent stopped"
        # Unsent events and screenshots waiting for the server. Copied (not moved) so Cyril still has them if we roll back.
        # health.json is the old agent's own status; the new agent writes its own.
        if (Test-Path $oldDataDir) {
            foreach ($item in (Get-ChildItem -Path $oldDataDir -Force | Where-Object { $_.Name -ne "health.json" })) {
                $dest = Join-Path $dataDir $item.Name
                if (-not (Test-Path $dest)) { Copy-Item -Path $item.FullName -Destination $dest -Recurse -Force; $copied++ }
            }
        }
        Ok "Unsent data copied to $dataDir ($copied item(s))"
        Register-AgentTask
        Start-ScheduledTask -TaskName $taskName
        # Check it really runs. With nobody logged on there is no desktop to start it in: the logon trigger starts it
        # at the next login, so there is nothing to wait for.
        if (Get-Process -Name "explorer" -ErrorAction SilentlyContinue) {
            $up = $false
            foreach ($i in 1..15) { Start-Sleep -Seconds 2; if (Get-Process -Name $procName -ErrorAction SilentlyContinue) { $up = $true; break } }
            if (-not $up) { throw "netmon.exe did not start within 30 seconds" }
            Start-Sleep -Seconds 5
            if (-not (Get-Process -Name $procName -ErrorAction SilentlyContinue)) { throw "netmon.exe exited right after starting" }
        }
        [IO.File]::WriteAllText($migratedMarker, "moved from Cyril at $([DateTimeOffset]::UtcNow.ToString('o'))")
        Ok "NetMon is running"
        Send-MigrationReport $true "moved from Cyril: old agent stopped and disabled, $copied data item(s) copied, NetMon started"
    } catch {
        $why = $_.Exception.Message
        Restore-Cyril $why
        Fail "Moving from Cyril to NetMon did not work ($why). Cyril has been switched back on and is still monitoring this PC."
    }
} else {
    Register-AgentTask
}
Ok "Task CA Registered"

# 5b. The updater: SYSTEM, daily. Re-runs this installer, which does nothing unless a new build is published.
# Its name and its action are both deliberately independent of the installed program (see $updaterName above): the
# server address and agent key are baked straight into the task's own command line (which Task Scheduler stores in
# C:\Windows\System32\Tasks, not in anything under NetMon), rather than read from $cfgFile at run time. So even if
# Program Files\NetMon and ProgramData\NetMon are both deleted -- by accident, or by someone trying to remove the
# agent without rights to Task Scheduler -- this task alone, at 03:00 or the next boot, reinstalls everything from
# scratch: it needs nothing left behind on disk to do so.
foreach ($n in $legacyUpdaterNames) { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue }
Unregister-ScheduledTask -TaskName $updaterName -Confirm:$false -ErrorAction SilentlyContinue
$installUrl = $ServerUrl.TrimEnd('/') + '/install.ps1?key=' + [uri]::EscapeDataString($AgentKey)
$cmd = '& ([scriptblock]::Create((irm ' + (PSQ $installUrl) + '))) -Unattended'
$uAction    = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$cmd`""
$uDaily     = New-ScheduledTaskTrigger -Daily -At "03:00" -RandomDelay (New-TimeSpan -Minutes 30)
$uBoot      = New-ScheduledTaskTrigger -AtStartup
$uBoot.Delay = "PT5M"   # a PC that was off/asleep at 03:00 checks soon after it starts (network needs a moment)
$uTrigger   = @($uDaily, $uBoot)
$uPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
# Laptops: run on battery too (the default is AC power only). A failed attempt (network not up yet, server busy)
# is retried 3 times, 10 minutes apart, instead of waiting for the next day.
$uSettings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10) -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $updaterName -Action $uAction -Trigger $uTrigger -Principal $uPrincipal -Settings $uSettings -Force | Out-Null
Ok "Task CU Registered"

# 5c. The recovery check: SYSTEM, every 15 minutes, but it only acts when the agent reports (data\health.json) that
#     its uploads have been failing for over an hour. Then it fetches this installer again, which installs a newer
#     agent build if one is published - so a PC stuck on a bug does not wait for 03:00, and a healthy PC never runs it.
#     The script lives in the read-only config folder (users cannot edit what SYSTEM runs); health.json is only read
#     as data. At most one attempt per 30 minutes. Registered once: re-registering it from inside its own run would
#     cut that run short.
$recoveryFile = Join-Path $cfgDir "recovery-check.ps1"
$recoveryBody = @'
# NetMon recovery check (written by install.ps1; runs as SYSTEM every 15 minutes).
$ErrorActionPreference = "Stop"
try {
    $dir   = Split-Path -Parent $MyInvocation.MyCommand.Path
    $now   = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $c = Get-Content (Join-Path $dir "config.json") -Raw | ConvertFrom-Json
    # Remote uninstall (the fast privileged path, every 15 min): if the server has ordered this machine removed, tear
    # the agent down now (this task runs as SYSTEM, which the user-level agent is not and cannot do) and report it,
    # instead of waiting for the daily updater. Runs before the health logic so it does not depend on anything failing.
    try { $dev = [Microsoft.Win32.RegistryKey]::OpenBaseKey("LocalMachine","Registry64").OpenSubKey("SOFTWARE\Microsoft\Cryptography").GetValue("MachineGuid") } catch { $dev = $env:COMPUTERNAME }
    try {
        $rm = Invoke-RestMethod -UseBasicParsing -Headers @{ "X-Api-Key" = $c.key } -TimeoutSec 20 -Uri ($c.server + "/api/agent/removal?device=" + [uri]::EscapeDataString("$dev"))
        if ("$rm" -match "remove=1") {
            & ([scriptblock]::Create((Invoke-RestMethod ($c.server + "/uninstall.ps1"))))
            $body = (@{ device = "$dev"; state = "removed"; detail = "removed by recovery task" } | ConvertTo-Json -Compress)
            try { Invoke-RestMethod -UseBasicParsing -Method Post -Headers @{ "X-Api-Key" = $c.key; "Content-Type" = "application/json" } -TimeoutSec 20 -Uri ($c.server + "/api/agent/removal-state") -Body $body | Out-Null } catch { }
            exit 0
        }
    } catch { }
    $h     = Get-Content (Join-Path $dir "data\health.json") -Raw | ConvertFrom-Json
    $since = [long]$h.failing_since
    if ($since -le 0 -or ($now - $since) -lt 3600) { exit 0 }
    $stamp = Join-Path $dir "recovery-last.txt"
    if ((Test-Path $stamp) -and (($now - [long](Get-Content $stamp -Raw)) -lt 1800)) { exit 0 }
    Set-Content -Path $stamp -Value $now
    & ([scriptblock]::Create((Invoke-RestMethod ($c.server + "/install.ps1?key=" + [uri]::EscapeDataString($c.key))))) -Unattended
} catch { exit 0 }
'@
[IO.File]::WriteAllText($recoveryFile, $recoveryBody, (New-Object Text.UTF8Encoding $false))
if (-not (Get-ScheduledTask -TaskName $recoveryName -ErrorAction SilentlyContinue)) {
    $rAction   = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$recoveryFile`""
    $rTrigger  = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
    $rSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $recoveryName -Action $rAction -Trigger $rTrigger -Principal $uPrincipal -Settings $rSettings -Force | Out-Null
    Ok "Task CR Registered"
}

# 5d. Once NetMon has taken over, switch off the old Cyril updater and recovery tasks so the two updaters never run
#     together. Disabled, not deleted, and never stopped (one of them may be running this very script). Runs on every
#     install (cheap, and it finishes the job if an earlier run stopped half-way); removing them is a later update.
if (Test-Path $migratedMarker) {
    foreach ($t in $oldUpdaterTasks) {
        $old = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if ($old -and $old.State -ne "Disabled") { Quiet { Disable-ScheduledTask -TaskName $t -ErrorAction Stop | Out-Null }; Ok "Old task '$t' switched off" }
    }
}

# 5e. Update 2: remove what the Cyril agent left behind. Only on a run AFTER the hand-over (so this is the NetMon updater
#     running, never a Cyril task that is about to be deleted), and only when NetMon has proved itself on this PC: its
#     own health.json (Cyril's was never copied) is under 3 days old and shows no upload failing. Otherwise Cyril's
#     files stay as the fallback and the next update asks again. A task that is running right now is left for next time.
function Test-NetMonHealthy {
    try {
        $h = Get-Content (Join-Path $dataDir "health.json") -Raw | ConvertFrom-Json
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        return ((([long]$h.failing_since) -eq 0) -and (($now - [long]$h.at) -lt 259200))
    } catch { return $false }
}
if ($cleanupOld) {
    if (-not (Test-NetMonHealthy)) {
        Step "NetMon has not reported healthy on this PC yet; leaving the old Cyril files for the next update"
    } else {
        $left = 0
        foreach ($t in (@($oldAgentTask) + $oldUpdaterTasks)) {
            $x = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
            if (-not $x) { continue }
            if ($x.State -eq "Running") { $left++; continue }
            try { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction Stop } catch { $left++ }
        }
        Get-Process -Name $oldProc -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        if ($left -eq 0) {
            foreach ($d in @($oldInstallDir, $oldCfgDir)) {
                if (Test-Path $d) { try { Remove-Item -Recurse -Force $d -ErrorAction Stop } catch { $left++ } }
            }
        }
        if ($left -eq 0) {
            Ok "Old Cyril tasks and files removed"
            Send-MigrationReport $true "old Cyril tasks, program folder and data folder removed" "cyril-leftovers" "removed"
        } else {
            Warn "Some old Cyril items could not be removed yet ($left); the next update will try again."
            Send-MigrationReport $false "$left old Cyril item(s) could not be removed (running or locked); will retry" "cyril-leftovers" "removed"
        }
    }
}

# 6. Earlier versions disabled Incognito/InPrivate by browser policy. The agent does not do that any more
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
if (-not (Get-Process -Name $procName -ErrorAction SilentlyContinue)) { Start-ScheduledTask -TaskName $taskName }
if ($Unattended) {
    if ($Silent) { Write-Host "NetMon installed$(if ($buildInfo) { " ($buildInfo)" }). The agent is running; it appears on the dashboard's Install agents page within a minute or two." }
    return
}

Step "Waiting for Server Confirmation"
$device = Get-DeviceId
$seen = $null
foreach ($i in 1..24) {
    Start-Sleep -Seconds 5
    try { $seen = Invoke-RestMethod -UseBasicParsing -Headers @{ "X-Api-Key" = $AgentKey } -TimeoutSec 15 -Uri "$ServerUrl/api/agent/seen?device=$([uri]::EscapeDataString("$device"))&since=$since" } catch { $seen = $null }
    if ($seen -and $seen.seen) { break }
}

function Show-Diagnostics {
    Write-Host ""
    Write-Host "  Diagnostics:" -ForegroundColor White
    $procs = Get-Process -Name $procName -ErrorAction SilentlyContinue
    if ($procs) { $procs | ForEach-Object { Write-Host "    netmon.exe pid $($_.Id) running in session $($_.SessionId)  (session 0 = background services, cannot see any desktop)" } }
    else { Write-Host "    netmon.exe is NOT running" }
    $info = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
    if ($info) { Write-Host "    task last run: $($info.LastRunTime), result: 0x$('{0:X}' -f $info.LastTaskResult)" }
    $log = Join-Path $dataDir "agent.log"
    if (Test-Path $log) { Write-Host "    last lines of $log :"; Get-Content $log -Tail 12 | ForEach-Object { Write-Host "      $_" } }
    else { Write-Host "    no agent log yet at $log (the agent never started)" }
    $crash = Join-Path $dataDir "crash.log"
    if (Test-Path $crash) { Write-Host "    the agent crashed on startup, $crash :" -ForegroundColor Red; Get-Content $crash -Tail 15 | ForEach-Object { Write-Host "      $_" } }
    elseif ($info -and $info.LastTaskResult -ne 0 -and -not $procs) { Write-Host "    the task ran but the agent exited at once (result 0x$('{0:X}' -f $info.LastTaskResult)): it crashed before it could log. Try running it by hand: & '$exe' --config '$cfgFile'" }
}

Write-Host ""
if ($seen -and $seen.seen -and $seen.with_app -gt 0) {
    Write-Host "Installation Successful $($seen.events)R/$($seen.with_app)A" -ForegroundColor Green
} elseif ($seen -and $seen.seen) {
    Warn "The server is receiving reports, but none contain an app name, so the agent cannot see the desktop."
    Show-Diagnostics
} else {
    Warn "Installed, but the server has not received a report from this machine yet."
    Show-Diagnostics
}
Write-Host ""
Write-Host ""
