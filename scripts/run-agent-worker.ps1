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

# Runs the deferred npm ci when a self-update left .self-update-pending.
# Only safe here: at this point the previous worker has exited, so no
# process pins the native DLLs inside node_modules (on Windows npm ci
# otherwise fails with EPERM mid-delete and leaves a broken tree).
function Invoke-PendingInstall {
    $marker = Join-Path $agentHost ".self-update-pending"
    if (!(Test-Path -LiteralPath $marker)) { return }
    Write-SupervisorLog "pending self-update detected; running npm ci"
    $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    try {
        Push-Location $agentHost
        & $npm ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit $LASTEXITCODE" }
        & $node scripts/patch-pi-brace-expansion.mjs
        & $npm run build
        if ($LASTEXITCODE -ne 0) { throw "build failed with exit $LASTEXITCODE" }
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $agentHost "package-lock.json")).Hash.ToLower()
        New-Item -ItemType Directory -Force -Path (Join-Path $agentHost "node_modules") | Out-Null
        Set-Content -LiteralPath (Join-Path $agentHost "node_modules\.installed-lock-hash") -Value $hash -NoNewline -Encoding ASCII
        Remove-Item -LiteralPath $marker -Force
        Write-SupervisorLog "pending self-update applied (lock $hash)"
    }
    catch {
        Write-SupervisorLog "pending self-update failed: $($_.Exception.Message)"
    }
    finally {
        Pop-Location
    }
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

    # Install deferred dependencies between worker runs, when the DLLs of
    # the previous worker are no longer pinned by any process.
    Invoke-PendingInstall

    Start-Sleep -Seconds ([Math]::Max($RestartDelaySeconds, 1))
}
