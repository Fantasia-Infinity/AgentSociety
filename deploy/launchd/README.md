# macOS launchd service for the Hub worker

A LaunchAgent makes the local Agent a persistent worker against the Hub:

- `ai.agentsociety.agent-worker` — runs
  `node agent-host/dist/src/cli.js worker` continuously; launchd restarts it
  if it exits (including after a supervised self-update restart).

The plist is installed at `~/Library/LaunchAgents/` and loads automatically
at login.

## Install / uninstall

```bash
cp deploy/launchd/ai.agentsociety.agent-worker.plist \
   ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.agentsociety.agent-worker.plist

# unload
launchctl bootout gui/$(id -u)/ai.agentsociety.agent-worker
```

## Notes

- The worker invokes `/opt/homebrew/bin/node` directly. Do not change the
  `ProgramArguments` back to the `agent` shell wrapper: launchd cannot exec
  scripts located under `~/Documents` on this machine (EPERM), while node +
  `dist/src/cli.js` works.
- The worker loads `../.env.agent` through `AGENT_ENV_FILE`; the Hub token is
  stored in the macOS keychain under the configured credential service.
- `AGENT_SELF_UPDATE` defaults to on, so the worker follows `origin/main`.
  Set it to `0` only when pinning a specific build (e.g. while a newer main
  is known-broken); remove the pin once main is healthy again.
