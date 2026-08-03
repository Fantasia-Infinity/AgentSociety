[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Stop"

# A pi TUI session exports these; a worker must never inherit them.
$env:AGENT_WORKER_SUPERVISED = "1"
Remove-Item Env:PI_PROVIDER -ErrorAction SilentlyContinue
Remove-Item Env:PI_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:AGENT_HUB_RUNTIME_DISABLED -ErrorAction SilentlyContinue

$node = (Get-Command node -ErrorAction Stop).Source
$agentHost = Join-Path $ProjectRoot "agent-host"
$entrypoint = Join-Path $agentHost "dist\src\cli.js"
if (!(Test-Path -LiteralPath $entrypoint)) {
    throw "Agent Host entrypoint not found: $entrypoint"
}

$logDirectory = Join-Path $ProjectRoot "worker-logs"
$supervisorLog = Join-Path $logDirectory "worker-supervisor.log"

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

function Write-SupervisorLog {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $supervisorLog -Value $line -Encoding UTF8
}

Write-SupervisorLog "agent worker supervisor started project=$ProjectRoot"

while ($true) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutLog = Join-Path $logDirectory "worker-$stamp.out.log"
    $stderrLog = Join-Path $logDirectory "worker-$stamp.err.log"
    Write-SupervisorLog "starting worker stdout=$stdoutLog stderr=$stderrLog"

    try {
        $process = Start-Process `
            -FilePath $node `
            -ArgumentList @($entrypoint, "worker") `
            -WorkingDirectory $agentHost `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -WindowStyle Hidden `
            -PassThru `
            -Wait
        Write-SupervisorLog "worker exited code=$($process.ExitCode)"
    }
    catch {
        Write-SupervisorLog "worker start failed error=$($_.Exception.Message)"
    }

    Start-Sleep -Seconds ([Math]::Max($RestartDelaySeconds, 1))
}
