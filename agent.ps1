$ErrorActionPreference = "Stop"

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

if ($Command -eq "setup") {
    & $NpmCommand --prefix $AgentHost run setup -- @Rest
    exit $LASTEXITCODE
}

$Config = Join-Path $RepositoryRoot ".env.agent"
$Modules = Join-Path $AgentHost "node_modules"
$Entrypoint = Join-Path $AgentHost "dist/src/cli.js"
$CompletionMarker = Join-Path $AgentHost ".setup-complete"
$DidSetup = $false
if (-not (Test-Path $CompletionMarker) -or -not (Test-Path $Config) -or -not (Test-Path $Modules) -or -not (Test-Path $Entrypoint)) {
    & $NpmCommand --prefix $AgentHost run setup
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $DidSetup = $true
}
if ($Command -eq "doctor" -and $DidSetup) {
    exit 0
}

switch ($Command) {
    { $_ -in "tui", "interactive", "local" } {
        & $NpmCommand --prefix $AgentHost run start -- $Command
        break
    }
    "worker" { & $NpmCommand --prefix $AgentHost run worker; break }
    { $_ -in "doctor", "sessions" } { & $NpmCommand --prefix $AgentHost run $Command; break }
    { $_ -in "register", "once", "observe", "attach", "bridge" } {
        & $NpmCommand --prefix $AgentHost run start -- $Command @Rest
        break
    }
    default {
        throw "Usage: .\\agent.ps1 [setup|tui|local|worker|bridge --adapter ID|doctor|sessions|register|once|observe ID|attach ID]"
    }
}
exit $LASTEXITCODE
