<#
Removes the Cyril agent completely and says what it removed. Needs an ELEVATED
PowerShell on purpose: a standard employee cannot remove it (see README, tamper resistance).

  & ([scriptblock]::Create((irm 'https://cyril.example.com/uninstall.ps1')))

Removes: the "Cyril Agent" and "Cyril Updater" scheduled tasks, the running process,
C:\Program Files\Cyril, C:\ProgramData\Cyril (config, key, agent log and spool), any
leftover spool/log files from older versions, and the Incognito setting if an older
installer had disabled it. Data already stored on the server is not touched.
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

# Tasks first, so nothing can restart the process we are about to stop.
foreach ($t in @("Cyril Agent", "Cyril Updater")) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        try { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
              Unregister-ScheduledTask -TaskName $t -Confirm:$false
              Row "Scheduled task '$t'" $true "removed" }
        catch { Row "Scheduled task '$t'" $false $_.Exception.Message; $failures++ }
    } else { Row "Scheduled task '$t'" $true "was not present" }
}

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

# Incognito/InPrivate: older installers disabled it by browser policy; restore exactly what they set.
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
