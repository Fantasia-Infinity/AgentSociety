[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Stop"

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$logDirectory = Join-Path $ProjectRoot "gateway-logs"
$supervisorLog = Join-Path $logDirectory "gateway-supervisor.log"

if (!(Test-Path -LiteralPath $python)) {
    throw "Gateway Python not found: $python"
}

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

function Write-SupervisorLog {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $supervisorLog -Value $line -Encoding UTF8
}

Write-SupervisorLog "gateway supervisor started project=$ProjectRoot"

while ($true) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutLog = Join-Path $logDirectory "gateway-$stamp.out.log"
    $stderrLog = Join-Path $logDirectory "gateway-$stamp.err.log"
    Write-SupervisorLog "starting gateway stdout=$stdoutLog stderr=$stderrLog"

    try {
        $process = Start-Process `
            -FilePath $python `
            -ArgumentList @("-m", "wechat_gateway") `
            -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -WindowStyle Hidden `
            -PassThru `
            -Wait
        Write-SupervisorLog "gateway exited code=$($process.ExitCode)"
    }
    catch {
        Write-SupervisorLog "gateway start failed error=$($_.Exception.Message)"
    }

    Start-Sleep -Seconds ([Math]::Max($RestartDelaySeconds, 1))
}
