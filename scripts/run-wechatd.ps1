[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Stop"

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$logDirectory = Join-Path $ProjectRoot "wechatd-logs"
$supervisorLog = Join-Path $logDirectory "wechatd-supervisor.log"

if (!(Test-Path -LiteralPath $python)) {
    throw "wechatd Python not found: $python"
}

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

function Write-SupervisorLog {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $supervisorLog -Value $line -Encoding UTF8
}

Write-SupervisorLog "wechatd supervisor started project=$ProjectRoot"

while ($true) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutLog = Join-Path $logDirectory "wechatd-$stamp.out.log"
    $stderrLog = Join-Path $logDirectory "wechatd-$stamp.err.log"
    Write-SupervisorLog "starting wechatd stdout=$stdoutLog stderr=$stderrLog"

    try {
        $process = Start-Process `
            -FilePath $python `
            -ArgumentList @("-m", "wechatd") `
            -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -WindowStyle Hidden `
            -PassThru `
            -Wait
        Write-SupervisorLog "wechatd exited code=$($process.ExitCode)"
    }
    catch {
        Write-SupervisorLog "wechatd start failed error=$($_.Exception.Message)"
    }

    Start-Sleep -Seconds ([Math]::Max($RestartDelaySeconds, 1))
}
