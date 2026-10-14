#Requires -RunAsAdministrator
# Removes the Cyril agent. Requires admin rights by design (see README on tamper-resistance).
$ErrorActionPreference = "SilentlyContinue"
Unregister-ScheduledTask -TaskName "Cyril Agent" -Confirm:$false
Stop-Process -Name "cyril" -Force
Remove-Item -Recurse -Force "$env:ProgramFiles\Cyril"
Remove-Item -Recurse -Force "$env:ProgramData\Cyril"
Write-Host "Cyril agent removed."
