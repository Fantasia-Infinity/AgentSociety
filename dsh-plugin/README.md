# @agent-society/dsh-agent-society

AgentSociety as a local DeepSeek Harness bundle.

Current scope:

- `agent-society-worker`: in-process Hub worker loop.
  - `ctx.agents.create()` for fresh task sessions.
  - `ctx.agents.resume()` for continuous sessions, including across process
    restarts.
  - Hub task claim / lease renewal via heartbeat / result reporting.
  - Hub controls: `agent.steer()` for `steer`, `agent.followup()` for
    `follow_up`, then Hub ACK.
  - Hub cancellation: `agent.cancel()` and cancelled Run reporting.
  - Per-task tool policy mapping:
    - `full`: all local dsh tools plus Hub MCP tools; `workspace-write`.
    - `read_only`: `read`, `read_image`, `glob`, `grep`, web, and external
      MCP tools; `read-only` sandbox.
    - `no_tools`: web and external MCP tools only; no local file/shell tools.
  - Session titles written to the dsh session log and reported as
    `dsh_session_title`.
  - Durable session transcript attached to the Hub as an artifact
    (`dsh-transcript-<run_id>-session.jsonl`).
- `agent-society-hub-mcp`: optional `mcp__agent-society__hub_*` dispatch tools.
- `agent-society-hub-tool-guard`: keeps those Hub tools in the assembled
  model catalog even when a preset (for example `anchored-standard`)
  filters the system-prompt tool list. Real `tools.restrict()` denials still
  win.

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

`./agent worker` now prefers this profile automatically when the profile
exists. Set `AGENT_WORKER_RUNTIME=pi` to force the legacy Pi worker.

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

With the checkout layout above, `./agent` (from the AgentSociety checkout)
launches this same source TUI automatically and passes the Hub MCP token.
Set `AGENT_TUI_RUNTIME=pi` to force the legacy Pi TUI, or
`AGENT_DSH_TUI_ROOT=/path/to/dsh-TUI` when the checkout lives elsewhere.

## Task input

The worker reads the standard `task.input.workspace` field. Optional fields:

| Field | Values | Meaning |
|---|---|---|
| `tool_policy` | `full` / `read_only` / `no_tools` | Overrides the process-wide `AGENT_SOCIETY_TOOL_POLICY` for this task |
| `title` | string | Preferred session title, truncated to 80 UTF-8 bytes |
| `reset_worker_session` | `true` | Discards the matching continuous session before starting |

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_SOCIETY_WORKER` | unset | Enable the in-process worker plugin |
| `AGENT_SOCIETY_HUB_URL` | — | Hub base URL |
| `AGENT_SOCIETY_HUB_TOKEN` | — | Hub bearer token |
| `AGENT_SOCIETY_HUB_MCP` | unset | Expose `mcp__agent-society__hub_*` tools |
| `AGENT_SOCIETY_WORKSPACE_ROOT` | process cwd | Allowed task workspace root |
| `AGENT_SOCIETY_SESSION_MODE` | `per_task` | `per_task` or `continuous` |
| `AGENT_SOCIETY_TOOL_POLICY` | `full` | Default `full` / `read_only` / `no_tools` |
| `AGENT_SOCIETY_SESSION_COMPRESSION` | `none` | Session log encoding; `zstd` opts into compressed logs |
| `AGENT_SOCIETY_POLL_SECONDS` | `20` | Task claim interval |
| `AGENT_SOCIETY_LEASE_SECONDS` | `300` | Hub lease duration |
| `AGENT_SOCIETY_ACTOR_ID` | `agent-society-<host>` | Actor identity |
| `AGENT_SOCIETY_NODE_ID` | hostname | Node identity |
| `AGENT_SOCIETY_PRINCIPAL_ID` | `human-<user>` | Principal identity |
| `AGENT_SOCIETY_DISPLAY_NAME` | `AgentSociety dsh worker on <host>` | Registration display name |
| `AGENT_SOCIETY_PROVIDER` | `deepseek-official` | dsh provider route |
| `AGENT_SOCIETY_MODEL` / `DSH_MODEL` | `deepseek-v4-flash` | dsh model id |
| `AGENT_SOCIETY_MAX_TOKENS` | `8192` | Per-request output cap |

## Observing runs

`./agent observe <run_id|task_id>` reads the dsh session transcript from the
Hub artifact attached by the worker when the transcript file is local to this
machine. The task and run results also carry `dsh_session_title`,
`dsh_tool_policy`, and `dsh_transcript_artifact_id`.

## Development

```bash
npm install
npm run check
npm run build
```
