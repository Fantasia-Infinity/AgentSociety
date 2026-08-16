# DeepSeek Harness 集成

AgentSociety 的默认 Agent 运行时现在是 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
（`dsh`）插件：`dsh-plugin/`（`@agent-society/dsh-agent-society`）。

主路径：

1. **dsh 作为交互 Agent**：`./agent` 默认打开带 AgentSociety bundle 的 dsh-TUI，
   Hub 工具以 `mcp__agent-society__hub_*` 暴露。
2. **dsh 作为 Hub worker**：`./agent worker` 默认启动
   `dsh --profile agent-society-worker`，任务在 dsh 进程内执行。
3. **一键安装**：
   [dsh-agent-society-combo](https://github.com/Fantasia-Infinity/dsh-agent-society-combo)
   固定 dsh / dsh-TUI / AgentSociety / preset 的 commit 与兼容 patch。

兼容通道仍保留：

- `./agent bridge --adapter dsh`：每个任务运行一次 `dsh --profile headless`。
- `./agent dsh-worker`：通过 dsh SDK JSON-RPC runtime 常驻执行 Hub 任务。
- `AGENT_TUI_RUNTIME=pi` / `AGENT_WORKER_RUNTIME=pi`：回退 Pi。

## dsh 进程内插件

`dsh-plugin/` 直接运行在 dsh 进程内，使用 `ctx.agents.create()` /
`ctx.agents.resume()`，因此具备跨进程 resume，也不再受 JSON-RPC SDK 的
cancel/steer 限制。

安装：

```bash
sh scripts/install-dsh-plugin.sh
```

Worker profile：

```bash
AGENT_SOCIETY_WORKER=1 \
AGENT_SOCIETY_HUB_URL=http://127.0.0.1:8090 \
AGENT_SOCIETY_HUB_TOKEN='<token>' \
AGENT_SOCIETY_WORKSPACE_ROOT=/path/to/workspaces \
dsh --profile agent-society-worker
```

dsh-TUI 本地集成（不改 dsh-TUI）：

```bash
mkdir -p ~/.dsh/plugins
ln -s /path/to/AgentSociety/dsh-plugin ~/.dsh/plugins/agent-society

AGENT_SOCIETY_HUB_MCP=1 \
AGENT_SOCIETY_HUB_URL=http://127.0.0.1:8090 \
AGENT_SOCIETY_HUB_TOKEN='<token>' \
node /path/to/dsh-TUI/bin/dsh-tui-local.js
```

`./agent`（即 `./agent tui`）现在会优先直接启动同级 checkout 里的
`dsh-TUI/scripts/run.ts`，并把 `~/.dsh/plugins/agent-society` 作为外部
bundle 加载；`dsh-TUI`、DeepSeek Harness checkout 或插件链接缺失时自动回退
Pi TUI。强制选择运行时：

```bash
AGENT_TUI_RUNTIME=dsh ./agent   # 缺 dsh-TUI 时直接报错
AGENT_TUI_RUNTIME=pi ./agent    # 强制 Pi TUI
AGENT_DSH_TUI_ROOT=/path/to/dsh-TUI ./agent
```

`./agent worker` 在检测到 `agent-society-worker` profile 时会默认启动
上面的插件进程，并把 `AGENT_REMOTE_TOOL_POLICY` 映射为
`AGENT_SOCIETY_TOOL_POLICY`；profile 不存在或 `dsh` 无法启动时自动回退到
Pi worker。强制走 Pi 时设置：

```bash
AGENT_WORKER_RUNTIME=pi ./agent worker
```

dsh-TUI 的 `/resume` 使用 `ctx.agents.resume`；continuous worker 也会把
`sessionId` 写入 `~/.dsh/agent-society-worker-sessions.json`，worker 重启后
恢复同一 session。

插件现在还会：

- 通过 `agent-society-hub-tool-guard` 在 system-prompt assembly 之后重新
  注入 `mcp__agent-society__hub_*`，因此 `anchored-standard` 这类会裁剪
  工具目录的 preset 也不会把 Hub 工具裁掉（真正的 `tools.restrict()`
  拒绝仍然生效）；
- 通过 `agent-society-web-tool-guard` 保持 dsh-base 的 `web_search` 可见；
  `AGENT_WEB_SEARCH` / `AGENT_DSH_WEB_SEARCH` 决定 CLI 是否给 worker 与
  TUI 注入 `AGENT_SOCIETY_WEB_SEARCH=1`，设为 `0` 会移除整个 DeepSeek
  搜索 provider 栈；

- 按任务 `input.tool_policy`（回退到 `AGENT_SOCIETY_TOOL_POLICY`）映射
  `full` / `read_only` / `no_tools`，并写入每个 session 的 `sandbox/mode`；
- 把任务标题写入 `session/title`，在 Run/Task 结果中返回
  `dsh_session_title`；
- flush 后把 dsh transcript 作为 Hub artifact 挂到 Run/Task
  （`dsh_transcript_artifact_id`），`./agent observe` 可读取本机文件 artifact；
- `input.action = "self_update"` 由 dsh worker 自己执行 `git pull --ff-only`、
  依赖安装与 `agent-host` / `dsh-plugin` 构建，任务落库后以退出码 `75`
  退出，`./agent worker` 父进程自动重启 dsh worker。

详见 [`dsh-plugin/README.md`](../dsh-plugin/README.md)。

以下各节仍保留外部 JSON-RPC/headless 通道，作为过渡和兼容路径。

## 版本

已针对 `@deepseek-ai/dsh` `0.1.0-rc.5` 实现。DeepSeek Harness 仍是 developer
preview，升级前请重新验证其 wire protocol。

## 派发端：`dsh-dispatch`

前提：本机可执行 `dsh`（已安装 `@deepseek-ai/dsh`，或通过
`AGENT_DSH_COMMAND` 指向源码 checkout）。

```bash
# 安装/指向 dsh 后：
./agent dsh-dispatch
```

该命令会用节点凭据启动：

```bash
dsh web --patch agent-host/dsh/agent-society.dsh.yml
```

启动后 dsh 会话中出现 `mcp__agent-society__hub_*` 工具。等价的手动配置：

```bash
AGENT_SOCIETY_HUB_URL=http://127.0.0.1:8090 \
AGENT_SOCIETY_HUB_MCP_TOKEN='<token>' \
dsh web --patch /path/to/AgentSociety/agent-host/dsh/agent-society.dsh.yml
```

## 执行端：Bridge 适配器

```bash
./agent bridge --adapter dsh --once
./agent bridge --adapter dsh
```

内置 manifest 假设 `dsh` 在 `PATH` 中。使用源码 checkout 时设置：

```bash
export AGENT_DSH_COMMAND='["node","/path/to/deepseek-harness/apps/cli/lib/bin.js"]'
./agent bridge --adapter dsh
```

适配器继承 Bridge 的通用行为：每个任务一个全新 headless session，最终
stdout 文本写回任务结果，超时/取消使用 SIGTERM → SIGKILL。dsh 凭据优先读
`DEEPSEEK_API_KEY`，否则读 `~/.dsh/.credentials.yaml`。

## 执行端：`dsh-worker`

```bash
./agent dsh-doctor    # 检查 dsh runtime、模型与 Hub 配置
./agent dsh-once      # 领取并执行一个任务
./agent dsh-worker    # 常驻 worker
```

默认使用随仓库发布的 `agent-host/config/dsh-worker.cordis.yml`。运行时默认
为 `dsh-jsonrpc-agent`。dsh 的裸插件名从**配置文件所在项目**解析，因此：

- 生产/独立运行建议使用 DeepSeek Harness Python SDK 的 bundled runtime
  （`dsh-jsonrpc-agent-pkg` 单文件可执行）或一个安装了所需 dsh 插件包的
  Node 项目；此时可直接把 `AGENT_DSH_CONFIG` 指向仓库内的配置文件。
- 使用源码 checkout 调试时，先把该配置文件复制到 checkout 的 `examples/`
  目录（那里的 `node_modules` 已含全部本地工具插件），例如：

```bash
cp agent-host/config/dsh-worker.cordis.yml \
   /path/to/deepseek-harness/examples/agent-society-worker.cordis.yml
export AGENT_DSH_RUNTIME_BIN=node
export AGENT_DSH_RUNTIME_ARGS='["/path/to/deepseek-harness/packages/examples/jsonrpc-demo/lib/bin.js"]'
export AGENT_DSH_CONFIG=/path/to/deepseek-harness/examples/agent-society-worker.cordis.yml
./agent dsh-worker
```

### 工具策略映射

| `AGENT_REMOTE_TOOL_POLICY` | dsh 工具 |
|---|---|
| `full` | bash（workspace-write 沙箱）、fs read/write/edit/search、todo、subagent |
| `read_only` | fs 只读 |
| `no_tools` | 无本地工具 |

`web_search` 与 Hub MCP 工具默认开启。如果当前 dsh runtime 安装中缺少对应
插件，AgentSociety 会在本次 worker 进程内自动关闭该能力并打印 warning，不
会阻断任务执行：

```bash
AGENT_DSH_WEB_SEARCH=0   # 显式关闭 web_search
AGENT_DSH_HUB_MCP=0      # 显式关闭 dsh worker 内的 Hub MCP 工具
```

权限模式默认 `workspace-write`；`full` 策略下需要完整本机权限时显式设置
`AGENT_DSH_PERMISSION_MODE=danger-full-access`。

### 已知边界

- **进程内插件**使用 `ctx.agents.resume`，支持跨进程 continuous session；
  `agent.steer` / `agent.followup` / `agent.cancel` 已接入 Hub：
  steer/follow-up 会 ACK 为 delivered，任务取消会 abort 当前 turn 并记录
  cancelled Run。
- **外部 JSON-RPC/headless 通道**仍受 dsh wire protocol 限制：无协议级
  cancel，不能跨进程 resume。
- `agent observe` 支持 dsh session transcript。dsh worker 默认使用未压缩
  JSONL（`AGENT_DSH_SESSION_COMPRESSION=none`），可直接读取。

## 环境变量

见 [`.env.agent.example`](../.env.agent.example) 中 DeepSeek Harness 小节。
