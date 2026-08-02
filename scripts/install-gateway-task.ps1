[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "WechatBotGateway"
)

$ErrorActionPreference = "Stop"

$runner = Join-Path $ProjectRoot "scripts\run-gateway.ps1"
if (!(Test-Path -LiteralPath $runner)) {
    throw "Gateway runner not found: $runner"
}

$userId = "$env:USERDOMAIN\$env:USERNAME"
$argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -ProjectRoot `"$ProjectRoot`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Keeps the Windows WeChat Gateway connected to Bot Core." | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Output "Scheduled task registered and started: $TaskName"
