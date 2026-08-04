# macOS launchd services for the Snellius Hub worker

Two LaunchAgents make the local Agent a persistent worker against the Hub
deployed on Snellius:

- `ai.agentsociety.hub-tunnel` — keeps the SSH tunnel
  `127.0.0.1:18090 -> HOST:18090` alive (restarts on failure).
- `ai.agentsociety.agent-worker` — runs
  `node agent-host/dist/src/cli.js worker` continuously; launchd restarts it
  if it exits (including after a supervised self-update restart).

Both plists are installed at `~/Library/LaunchAgents/` and load automatically
at login.

## Install / uninstall

```bash
cp deploy/launchd/ai.agentsociety.hub-tunnel.plist \
   deploy/launchd/ai.agentsociety.agent-worker.plist \
   ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.agentsociety.hub-tunnel.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.agentsociety.agent-worker.plist

# unload
launchctl bootout gui/$(id -u)/ai.agentsociety.agent-worker
launchctl bootout gui/$(id -u)/ai.agentsociety.hub-tunnel
```

## Notes

- The worker invokes `/opt/homebrew/bin/node` directly. Do not change the
  `ProgramArguments` back to the `agent` shell wrapper: launchd cannot exec
  scripts located under `~/Documents` on this machine (EPERM), while node +
  `dist/src/cli.js` works.
- The worker loads `../.env.agent` through `AGENT_ENV_FILE`; the Hub token is
  stored in the macOS keychain as `AgentSociety Hub Snellius`.
- `AGENT_SELF_UPDATE=0` is set in `.env.agent` so the worker stays on the
  `publicserver` build instead of pulling `origin/main`.
