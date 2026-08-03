[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$BinDirectory = (Join-Path $HOME ".local\bin")
)

$ErrorActionPreference = "Stop"

$agentScript = Join-Path $ProjectRoot "agent.ps1"
if (!(Test-Path -LiteralPath $agentScript)) {
    throw "Agent entrypoint not found: $agentScript"
}

New-Item -ItemType Directory -Force -Path $BinDirectory | Out-Null
$commandPath = Join-Path $BinDirectory "agent.cmd"
$escapedRoot = $ProjectRoot.Replace('"', '\"')
$contents = @"
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$escapedRoot\agent.ps1" %*
exit /b %ERRORLEVEL%
"@.TrimStart()
Set-Content -LiteralPath $commandPath -Value $contents -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = @($userPath -split ";" | Where-Object { $_ })
if ($pathEntries -notcontains $BinDirectory) {
    $nextPath = (($pathEntries + $BinDirectory) -join ";")
    [Environment]::SetEnvironmentVariable("Path", $nextPath, "User")
}

Write-Output "Installed: $commandPath"
Write-Output "Open a new PowerShell window before running: agent"
