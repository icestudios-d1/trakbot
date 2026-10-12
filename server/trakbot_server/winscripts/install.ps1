#Requires -RunAsAdministrator
<#
TrakBot agent installer. Needs only PowerShell on the target machine (no git,
no npm, no Python) -- those are only needed once, by IT, to build the .exe via
windows\build.ps1.

Usage (run elevated on the target machine):
  $cfg = Invoke-RestMethod https://trakbot.example.com/install.ps1
  & ([scriptblock]::Create($cfg)) -ServerUrl https://trakbot.example.com -AgentKey <agent key>

Or save it first if you'd rather not run from memory:
  iwr https://trakbot.example.com/install.ps1 -OutFile install.ps1
  Set-ExecutionPolicy -Scope Process Bypass -Force
  .\install.ps1 -ServerUrl https://trakbot.example.com -AgentKey <agent key>
#>
param(
    [Parameter(Mandatory = $true)][string]$ServerUrl,
    [Parameter(Mandatory = $true)][string]$AgentKey,
    [string]$User = $null  # defaults to the logged-in user if omitted
)
$ErrorActionPreference = "Stop"
$ServerUrl = $ServerUrl.TrimEnd("/")

$installDir = Join-Path $env:ProgramFiles "TrakBot"
$cfgDir = Join-Path $env:ProgramData "TrakBot"
New-Item -ItemType Directory -Force -Path $installDir, $cfgDir | Out-Null

Write-Host "Downloading agent from $ServerUrl ..."
Invoke-WebRequest -Uri "$ServerUrl/dist/trakbot-agent.exe" -OutFile "$installDir\trakbot-agent.exe"
Invoke-WebRequest -Uri "$ServerUrl/dist/trakbot-extension.zip" -OutFile "$installDir\trakbot-extension.zip"
Expand-Archive -Force "$installDir\trakbot-extension.zip" "$installDir\extension"

# Config (contains the shared agent API key) is readable only by SYSTEM/Administrators.
$cfg = @{ server = $ServerUrl; key = $AgentKey }
if ($User) { $cfg.user = $User }
$cfg | ConvertTo-Json | Set-Content "$cfgDir\config.json" -Encoding UTF8
icacls $cfgDir /inheritance:r | Out-Null
icacls $cfgDir /grant:r "SYSTEM:(OI)(CI)F" "BUILTIN\Administrators:(OI)(CI)F" | Out-Null

# Run at startup as SYSTEM (not the logged-in user) so a non-admin employee
# can't stop or uninstall it without admin rights, and it restarts if killed.
Unregister-ScheduledTask -TaskName "TrakBot Agent" -Confirm:$false -ErrorAction SilentlyContinue
$action    = New-ScheduledTaskAction -Execute "$installDir\trakbot-agent.exe" -Argument "--config `"$cfgDir\config.json`""
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "TrakBot Agent" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "TrakBot Agent"

# Optional, recommended: force-install the companion extension via Chrome/Edge
# enterprise policy so it runs without each employee installing it themselves,
# and/or disable Incognito entirely (simplest fix for the private-browsing gap --
# screenshots still cover whatever you don't block this way).
foreach ($browserKey in @("Google\Chrome", "Microsoft\Edge")) {
    $p = "HKLM:\SOFTWARE\Policies\$browserKey"
    New-Item -Path $p -Force | Out-Null
    Set-ItemProperty -Path $p -Name "IncognitoModeAvailability" -Value 1  # 1 = Disabled
}

Write-Host "TrakBot agent installed and running as a SYSTEM scheduled task."
Write-Host "Verify: Get-ScheduledTask 'TrakBot Agent' | Get-ScheduledTaskInfo"
