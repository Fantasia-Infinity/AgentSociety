# macOS launchd service for the Hub worker

A LaunchAgent makes the local Agent a persistent worker against the Hub:

- `ai.agentsociety.agent-worker` — runs
  `node agent-host/dist/src/cli.js worker` continuously; launchd restarts it
  if it exits (including after a supervised self-update restart).

The plist is installed at `~/Library/LaunchAgents/` and loads automatically
at login. The repository tracks only a template with `__PLACEHOLDER__` values;
the real plist contains absolute machine paths and must be generated locally
(a future `agent service install` does this automatically).

## Install / uninstall (manual)

```bash
mkdir -p ~/Library/LaunchAgents
sed -e 's|__NODE__|/opt/homebrew/bin/node|g' \
    -e 's|__CLI__|/Users/YOU/Documents/AgentSociety/agent-host/dist/src/cli.js|g' \
    -e 's|__ENV_FILE__|/Users/YOU/Documents/AgentSociety/.private/env/agent.env|g' \
    -e 's|__WORKSPACE__|/Users/YOU/Documents/AgentSociety|g' \
    -e 's|__LOG_DIR__|/Users/YOU/.pi/agent/logs|g' \
    deploy/launchd/ai.agentsociety.agent-worker.plist.example \
    > ~/Library/LaunchAgents/ai.agentsociety.agent-worker.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.agentsociety.agent-worker.plist

# unload
launchctl bootout gui/$(id -u)/ai.agentsociety.agent-worker
```

## Notes

- The worker invokes `/opt/homebrew/bin/node` directly. Do not change the
  `ProgramArguments` back to the `agent` shell wrapper: launchd cannot exec
  scripts located under `~/Documents` on this machine (EPERM), while node +
  `dist/src/cli.js` works.
- The worker loads `.private/env/agent.env` (legacy `.env.agent` still works)
  through `AGENT_ENV_FILE`; the Hub token is
  stored in the macOS keychain under the configured credential service.
- `AGENT_SELF_UPDATE` defaults to on, so the worker follows `origin/main`.
  Set it to `0` only when pinning a specific build (e.g. while a newer main
  is known-broken); remove the pin once main is healthy again.
