#Requires -RunAsAdministrator
# Removes the TrakBot agent. Requires admin rights by design (see README on tamper-resistance).
$ErrorActionPreference = "SilentlyContinue"
Unregister-ScheduledTask -TaskName "TrakBot Agent" -Confirm:$false
Stop-Process -Name "trakbot-agent" -Force
Remove-Item -Recurse -Force "$env:ProgramFiles\TrakBot"
Remove-Item -Recurse -Force "$env:ProgramData\TrakBot"
Write-Host "TrakBot agent removed."
