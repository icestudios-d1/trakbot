<#
Removes the Cyril agent completely and says what it removed. Needs an ELEVATED
PowerShell on purpose: a standard employee cannot remove it (see README, tamper resistance).

  & ([scriptblock]::Create((irm 'https://cyril.example.com/uninstall.ps1')))

Removes: the "Cyril Agent" scheduled task, the running process, C:\Program Files\Cyril,
C:\ProgramData\Cyril (config + key), the agent's local spool/log files, and any browser
policy value the installer itself created. Policy values that were already there before
Cyril are left alone. Data already stored on the server is not touched.
#>
$ErrorActionPreference = "Stop"

function Row($name, $done, $note) {
    $mark = if ($done) { "[ok]" } else { "[!!]" }
    $color = if ($done) { "Green" } else { "Yellow" }
    Write-Host ("  {0} {1}{2}" -f $mark, $name, $(if ($note) { " - $note" } else { "" })) -ForegroundColor $color
}

Write-Host ""
Write-Host "Cyril agent uninstaller" -ForegroundColor White
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "  [xx] Run this in an elevated PowerShell (right-click PowerShell, 'Run as administrator')." -ForegroundColor Red
    throw "Cyril uninstall needs administrator rights."
}

$installDir = Join-Path $env:ProgramFiles "Cyril"
$cfgDir     = Join-Path $env:ProgramData "Cyril"
$regKey     = "HKLM:\SOFTWARE\Cyril"
$failures   = 0

# Task first, so it cannot restart the process we are about to stop.
if (Get-ScheduledTask -TaskName "Cyril Agent" -ErrorAction SilentlyContinue) {
    try { Stop-ScheduledTask -TaskName "Cyril Agent" -ErrorAction SilentlyContinue
          Unregister-ScheduledTask -TaskName "Cyril Agent" -Confirm:$false
          Row "Scheduled task" $true "removed" }
    catch { Row "Scheduled task" $false $_.Exception.Message; $failures++ }
} else { Row "Scheduled task" $true "was not present" }

$procs = Get-Process -Name "cyril" -ErrorAction SilentlyContinue
if ($procs) {
    $procs | Stop-Process -Force
    Start-Sleep -Seconds 2
    if (Get-Process -Name "cyril" -ErrorAction SilentlyContinue) { Row "Agent process" $false "still running"; $failures++ }
    else { Row "Agent process" $true "stopped" }
} else { Row "Agent process" $true "was not running" }

foreach ($d in @($installDir, $cfgDir)) {
    if (Test-Path $d) {
        try { Remove-Item -Recurse -Force $d; Row $d $true "deleted" }
        catch { Row $d $false $_.Exception.Message; $failures++ }
    } else { Row $d $true "was not present" }
}

# Local spool/log live in the profile of whoever ran the agent (SYSTEM for the scheduled task).
$profiles = @("$env:SystemRoot\System32\config\systemprofile", $env:USERPROFILE)
foreach ($pr in $profiles) {
    Get-ChildItem -Path $pr -Filter ".cyril-*" -Force -ErrorAction SilentlyContinue | ForEach-Object {
        try { Remove-Item -Force $_.FullName; Row $_.FullName $true "deleted" } catch { Row $_.FullName $false $_.Exception.Message; $failures++ }
    }
}

# Browser policy: only what the installer created.
if (Test-Path $regKey) {
    $set = (Get-ItemProperty -Path $regKey -Name "IncognitoPolicySetFor" -ErrorAction SilentlyContinue).IncognitoPolicySetFor
    foreach ($b in @("$set" -split ";" | Where-Object { $_ })) {
        $p = "HKLM:\SOFTWARE\Policies\$b"
        try { Remove-ItemProperty -Path $p -Name "IncognitoModeAvailability" -ErrorAction Stop; Row "Incognito policy ($b)" $true "restored" }
        catch { Row "Incognito policy ($b)" $true "already gone" }
    }
    Remove-Item -Recurse -Force $regKey
}

Write-Host ""
if ($failures) { Write-Host "Cyril agent partly removed: $failures item(s) need attention (see above)." -ForegroundColor Yellow }
else { Write-Host "Cyril agent removed. Nothing is left running on this machine." -ForegroundColor Green }
Write-Host "Its past activity stays on the server until you delete it there."
Write-Host ""
