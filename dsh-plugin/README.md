# @agent-society/dsh-agent-society

AgentSociety as a local DeepSeek Harness bundle.

Current scope:

- `agent-society-worker`: in-process Hub worker loop.
  - `ctx.agents.create()` for fresh task sessions.
  - `ctx.agents.resume()` for continuous sessions, including across process
    restarts.
  - Hub task claim / lease renewal via heartbeat / result reporting.
- `agent-society-hub-mcp`: optional `mcp__agent-society__hub_*` dispatch tools.

The Python Hub and its REST contract remain unchanged.

## Local installation

### Worker profile

```bash
dsh plugin --profile agent-society-worker \
  add /path/to/AgentSociety/dsh-plugin

AGENT_SOCIETY_WORKER=1 \
AGENT_SOCIETY_HUB_URL=http://127.0.0.1:8090 \
AGENT_SOCIETY_HUB_TOKEN='<hub-token>' \
AGENT_SOCIETY_WORKSPACE_ROOT=/path/to/workspaces \
dsh --profile agent-society-worker
```

### dsh-TUI integration

The local `dsh-TUI` source launcher scans `$DSH_HOME/plugins/<name>/` before
its own bundle layer:

```bash
mkdir -p ~/.dsh/plugins
ln -s /path/to/AgentSociety/dsh-plugin ~/.dsh/plugins/agent-society

AGENT_SOCIETY_HUB_MCP=1 \
AGENT_SOCIETY_HUB_URL=http://127.0.0.1:8090 \
AGENT_SOCIETY_HUB_TOKEN='<hub-token>' \
node /path/to/dsh-TUI/bin/dsh-tui-local.js
```

Set `AGENT_SOCIETY_WORKER=0` (or leave unset) so the interactive TUI does not
claim Hub tasks.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_SOCIETY_WORKER` | unset | Enable the in-process worker plugin |
| `AGENT_SOCIETY_HUB_URL` | — | Hub base URL |
| `AGENT_SOCIETY_HUB_TOKEN` | — | Hub bearer token |
| `AGENT_SOCIETY_WORKSPACE_ROOT` | process cwd | Allowed task workspace root |
| `AGENT_SOCIETY_SESSION_MODE` | `per_task` | `per_task` or `continuous` |
| `AGENT_SOCIETY_POLL_SECONDS` | `20` | Task claim interval |
| `AGENT_SOCIETY_LEASE_SECONDS` | `300` | Hub lease duration |
| `AGENT_SOCIETY_ACTOR_ID` | `agent-society-<host>` | Actor identity |
| `AGENT_SOCIETY_NODE_ID` | hostname | Node identity |
| `AGENT_SOCIETY_PRINCIPAL_ID` | `human-<user>` | Principal identity |
| `DSH_MODEL` | `deepseek-v4-flash` | dsh model id |

## Development

```bash
npm install
npm run check
npm run build
```
