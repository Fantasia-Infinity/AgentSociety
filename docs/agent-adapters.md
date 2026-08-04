# Agent 适配器：让任意 Agent 接入 Hub

## 两层插件面

Hub 保持纯协调者：不执行任务、不提供沙箱。任意 Agent 通过两层接口接入：

1. **接收/执行（Bridge）**：`agent-host` 内置一个通用 CLI 适配器 worker
   （`./agent bridge --adapter <id>`）。它按 manifest 调用任意有非交互 CLI 的
   Agent（首版内置 `opencode`、`codex`、`generic`）。Pi 继续使用原有
   `./agent worker` 路径，行为不变。
2. **派发（MCP）**：Hub 暴露 MCP Server（stdio 与 HTTP `/mcp`），
   Codex/OpenCode/Claude 等 MCP 客户端可以直接 `hub_create_task`、
   `hub_list_tasks`、`hub_get_task`、`hub_get_task_events`、
   `hub_cancel_task`、`hub_list_actors`、`hub_list_nodes`。

任务生命周期 v1：一次执行 + 进度事件 + 取消。非 Pi 适配器暂不支持
steer/follow-up；连续 worker session 是可选能力（见下文）。

## Bridge 使用

```bash
# 查看可用适配器
./agent bridge --help  # 或直接运行查看错误信息里的列表

# 用 OpenCode 作为 worker 持续领取任务
./agent bridge --adapter opencode

# 用 Codex CLI，单次领取一条任务调试
./agent bridge --adapter codex --once

# 自定义适配器目录（manifest 与内置目录同名时优先）
AGENT_HUB_ADAPTER_DIR=/path/to/adapters ./agent bridge --adapter my-agent
```

身份默认按 `AGENT_HUB_ADAPTER`/`--adapter` 派生：
`actor_id=<adapter>-<node>`、`node_id=<node>-<adapter>`，可用
`AGENT_ACTOR_ID`、`AGENT_NODE_ID` 覆盖。如果使用 node token，token 的
`actor_id`/`node_id` 必须与最终身份一致。

连续会话复用现有环境变量：`AGENT_WORKER_SESSION_MODE=continuous`、
`AGENT_WORKER_SESSION_MAX_TASKS`、`AGENT_WORKER_SESSION_MAX_AGE_HOURS`，
任务 `input.reset_worker_session=true` 可强制新建。只有 manifest 声明
`session.resume=true` 才启用；否则自动退回 per-task。

## Manifest 规范

内置 manifest 在 `agent-host/adapters/*.json`，自定义目录放
`<adapter-id>.json`：

```json
{
  "id": "opencode",
  "display_name": "OpenCode",
  "capabilities": ["code"],
  "command": ["opencode", "run"],
  "args": ["--json", "{prompt}"],
  "env": {},
  "result_mode": "stdout_json",
  "timeout_seconds": 3600,
  "cancel_grace_seconds": 10,
  "session": {
    "resume": true,
    "new_args": ["--json", "{prompt}"],
    "resume_args": ["--json", "--session", "{session_id}", "{prompt}"],
    "result_field": "session_id",
    "discovery_glob": ".opencode/sessions/*.jsonl"
  }
}
```

字段：

- `command`：可执行文件 + 固定参数。
- `args` / `session.new_args` / `session.resume_args`：追加参数，支持占位符
  `{task_file}`（任务信封路径）、`{prompt}`（任务目标文本）、`{workspace}`、
  `{session_id}`、`{sandbox}`（默认 `workspace-write`，可用环境变量
  `AGENT_ADAPTER_SANDBOX` 覆盖为 `read-only` 或 `danger-full-access`）。
- `env`：附加环境变量。
- `result_mode`：`file` 表示读取信封目录下的 `AGENT_RESULT.json`；
  `stdout_json` 表示解析 stdout JSON（解析失败时把 stdout 当纯文本结果）。
- `session.resume_args` 在 `session.resume=true` 时必须提供。
- `session.discovery_glob`：拿不到 `session_id` 时，按该 glob 找最新
  session 文件（相对 workspace，或 `~/` 开头）；openCode 用
  `.opencode/sessions/*.jsonl`，Codex 用 `~/.codex/sessions/*.jsonl`。

## 任务信封与结果契约

Bridge 在每个任务的工作目录 `.agenthub/<run_id>/` 写入 `AGENT_TASK.json`，
并把路径注入 `AGENT_HUB_TASK_FILE`（另有 `AGENT_HUB_TASK_ID`、
`AGENT_HUB_RUN_ID`、`AGENT_HUB_WORKSPACE`、`AGENT_HUB_OBJECTIVE`、
`AGENT_HUB_SESSION_ID` 环境变量；token 永不进入文件）。

```json
{
  "task_id": "task_...",
  "run_id": "run_...",
  "objective": "检查仓库并返回测试结果",
  "input": { "workspace": "." },
  "workspace": "/abs/path",
  "capabilities": ["code"],
  "session_id": "ses_...",
  "continue": true
}
```

Agent 在信封目录写 `AGENT_RESULT.json`（或 stdout 输出 JSON）：

```json
{
  "status": "completed",
  "message": "optional",
  "result": { "extra": "field" },
  "text": "human/agent-readable result",
  "session_id": "ses_new_...",
  "artifacts": [
    { "path": "report.md", "name": "report.md", "media_type": "text/markdown" }
  ]
}
```

约定：

- `status` 缺省按退出码判定；非零退出或超时 → `failed`。
- `artifacts[].path` 相对 workspace；Hub 配置了对象存储时 Bridge 会上传内容，
  否则只登记本地 `file://` 元数据。
- 取消：Bridge 每 2s 轮询任务状态，收到 `cancelled` 后 SIGTERM，宽限期后
  SIGKILL，并把 Run 标记为 cancelled。

## MCP 接入

HTTP 端点：`POST /mcp`（Bearer token 与 `/v1/hub/*` 相同，租户 token 自动
隔离租户数据）。stdio：`agent-hub-mcp`（从环境变量读取 Hub 配置，供本地
调试）。默认开启，可用 `AGENT_HUB_ENABLE_MCP=false` 关闭。

```bash
# 本地 stdio 调试
PYTHONPATH=src AGENT_HUB_TOKEN=... python3 -m agent_hub.mcp_server
```

MCP 客户端配置示例（streamable HTTP）：

```json
{
  "mcpServers": {
    "hub": {
      "url": "https://hub.example.com/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

## Codex 接入

Codex 可以同时扮演两种角色，互不冲突：

1. **派发端（MCP 客户端）**：配置 Hub 的 `/mcp` 后，Codex 会话内直接获得
   `hub_create_task`、`hub_list_tasks`、`hub_get_task`、
   `hub_get_task_events`、`hub_cancel_task`、`hub_list_actors`、
   `hub_list_nodes` 工具。
2. **执行端（Bridge worker）**：`./agent bridge --adapter codex` 持续领取任务，
   用 `codex exec` 执行；连续会话模式下复用 `codex exec resume <session_id>`。

本机 MCP 配置（全局，之后新开 Codex 会话即可见）：

```bash
codex mcp add hub --url http://127.0.0.1:8090/mcp \
  --header "Authorization: Bearer <hub-token>"
codex mcp list --json        # 确认 hub enabled
```

配置完成后需要重启 Codex 会话让工具加载；Hub 未运行时工具调用会报连接失败。

执行端示例：

```bash
AGENT_HUB_URL=http://127.0.0.1:8090 \
AGENT_HUB_NODE_TOKEN=<node-token> \
AGENT_WORKER_SESSION_MODE=continuous \
./agent bridge --adapter codex
```

内置 `codex` 适配器使用非交互参数：

- `--skip-git-repo-check`：允许在非 Git 目录执行。
- `--sandbox workspace-write`：允许在 workspace 内写文件；需要更宽松或更严格
  策略时设置 `AGENT_ADAPTER_SANDBOX`（例如 `danger-full-access`）。
- `--json`：以 JSONL 事件输出，Bridge 从中提取最终 `agent_message` 文本和
  session id。

连续会话从 `codex exec --json` 的 `thread.started.thread_id` 事件读取 session
id；拿不到时按 `~/.codex/sessions/**/*.jsonl` 找最新文件并从
`rollout-<时间>-<uuid>.jsonl` 文件名中提取 UUID。恢复时参数顺序为
`codex exec --sandbox ... --skip-git-repo-check resume <session_id> --json <prompt>`
（`--sandbox` 必须放在 `resume` 子命令之前）。

Codex 执行时使用 `~/.codex/config.toml` 的模型/认证配置（本机当前为
DeepSeek provider）。想为任务指定不同模型，可以复制内置 manifest 到
`AGENT_HUB_ADAPTER_DIR` 后在 `args` 里加 `-m <model>` 或
`-c model="<model>"`。

关于 GUI：`codex exec` 的会话文件写在 `~/.codex/sessions/`，与 Codex 桌面端
共用同一存储，因此会出现在 Codex 的历史/会话列表中（应用需要重新索引或重启
后可见），也可以随时用 `codex exec resume <uuid>` 手动恢复。Bridge 只记录
session id 并在任务间透传，不会伪造上下文。

## 新增一个适配器

1. 在 `agent-host/adapters/` 或 `AGENT_HUB_ADAPTER_DIR` 添加 `<id>.json`。
2. 确认 Agent 有非交互 CLI，把目标文本放到 `{prompt}`，结构化输入可从
   `AGENT_HUB_TASK_FILE` 读取。
3. 让 Agent 输出 `AGENT_RESULT.json`（`result_mode=file`）或 stdout JSON
   （`result_mode=stdout_json`）。
4. 如需连续会话，实现会话恢复并配置 `session` 块。

## 安全边界

- Bridge 直接在本机执行 Agent CLI，workspace 限制在
  `AGENT_WORKSPACE_ROOT` 内；没有容器沙箱。
- 使用 node token 时只能领取本租户任务；Hub 不向 NAT/防火墙后的设备推送任务。
- 任务目标文本会被拼进 CLI 参数；适配器 manifest 属于设备所有者控制的可信配置。
