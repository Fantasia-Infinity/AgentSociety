[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "AgentSocietyWorker"
)

$ErrorActionPreference = "Stop"

$runner = Join-Path $ProjectRoot "scripts\run-agent-worker.ps1"
if (!(Test-Path -LiteralPath $runner)) {
    throw "Agent worker runner not found: $runner"
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
    -Description "Keeps the AgentSociety Hub worker claiming tasks for this node." | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Output "Scheduled task registered and started: $TaskName"
