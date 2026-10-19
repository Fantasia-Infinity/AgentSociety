$ErrorActionPreference = "Stop"

# This wrapper is the explicit Combo source-mode entrypoint.
$env:AGENT_RUNTIME_MODE = "source"

$RepositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentHost = Join-Path $RepositoryRoot "agent-host"
$Command = if ($args.Count -gt 0) { $args[0] } else { "tui" }
$Rest = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js 22.19 or newer and npm are required."
}

$NpmCommand = if (Get-Command npm.cmd -ErrorAction SilentlyContinue) {
    "npm.cmd"
} elseif (Get-Command npm -ErrorAction SilentlyContinue) {
    "npm"
} else {
    throw "Node.js 22.19 or newer and npm are required."
}

$Modules = Join-Path $AgentHost "node_modules"
$Entrypoint = Join-Path $AgentHost "dist/src/cli.js"

function Get-AgentConfigPath {
    if ($env:AGENT_ENV_FILE) { return $env:AGENT_ENV_FILE }
    $DshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $HOME ".dsh" }
    return Join-Path $DshHome ".env"
}

function Test-AgentConfig {
    return (
        (Test-Path (Get-AgentConfigPath)) -or
        (Test-Path (Join-Path $RepositoryRoot ".private\env\agent.env")) -or
        (Test-Path (Join-Path $RepositoryRoot ".env.agent"))
    )
}

function Invoke-AgentBootstrap {
    param([switch]$Force)
    if ($Force -or -not (Test-Path $Modules) -or -not (Test-Path $Entrypoint)) {
        & $NpmCommand --prefix $AgentHost run bootstrap
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}

if ($Command -in "bootstrap", "repair") {
    Invoke-AgentBootstrap -Force
    exit $LASTEXITCODE
}

if ($Command -in "setup", "configure") {
    Invoke-AgentBootstrap
    & $NpmCommand --prefix $AgentHost run setup -- @Rest
    exit $LASTEXITCODE
}

Invoke-AgentBootstrap
if ($Command -ne "doctor" -and $Command -ne "sessions" -and -not (Test-AgentConfig)) {
    & $NpmCommand --prefix $AgentHost run setup
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

switch ($Command) {
    { $_ -in "tui", "interactive", "local" } {
        & $NpmCommand --prefix $AgentHost run start -- $Command
        break
    }
    "worker" { & $NpmCommand --prefix $AgentHost run worker; break }
    { $_ -in "web", "web-bridge" } { & $NpmCommand --prefix $AgentHost run start -- $Command @Rest; break }
    "doctor" { & node $Entrypoint "doctor" @Rest; break }
    { $_ -in "sessions", "dsh-doctor" } { & $NpmCommand --prefix $AgentHost run start -- $Command @Rest; break }
    "update" { & $NpmCommand --prefix $AgentHost run start -- $Command @Rest; break }
    { $_ -in "register", "once", "observe", "attach", "bridge", "connect", "dsh-worker", "dsh-once", "dsh-dispatch" } {
        & $NpmCommand --prefix $AgentHost run start -- $Command @Rest
        break
    }
    default {
        throw "Usage: .\agent.ps1 [setup|configure|bootstrap|repair|update|tui|web|web-bridge|local|worker|connect|bridge --adapter ID|doctor [--live] [--json]|dsh-doctor|sessions|register|once|observe ID|attach ID|steer|follow-up|cancel|control|dsh-worker|dsh-once|dsh-dispatch]"
    }
}
exit $LASTEXITCODE
