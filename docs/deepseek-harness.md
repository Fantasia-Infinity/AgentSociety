# DeepSeek Harness 集成

AgentSociety 与 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
（`dsh`）之间有两条内置通道：

1. **dsh 作为派发端**：`dsh web` 会话内加载 AgentSociety Hub 的 MCP 工具
   （`hub_create_task`、`hub_list_tasks`、`hub_get_task` 等）。
2. **dsh 作为执行端**：
   - `./agent bridge --adapter dsh`：每个任务运行一次 `dsh --profile headless`，
     适合快速接入，无流式进度和连续会话。
   - `./agent dsh-worker`：通过 dsh SDK JSON-RPC runtime 常驻执行 Hub 任务，
     支持文本流式、工具策略映射和同进程连续会话。

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

`web_search` 与 Hub MCP 工具为可选能力，默认不加载（避免要求额外的 dsh
插件包），通过环境变量显式开启：

```bash
AGENT_DSH_WEB_SEARCH=1   # 需要 dsh-web-search-deepseek / dsh-tool-web 可解析
AGENT_DSH_HUB_MCP=1      # 需要 dsh-mcp-client 可解析
```

权限模式默认 `workspace-write`；`full` 策略下需要完整本机权限时显式设置
`AGENT_DSH_PERMISSION_MODE=danger-full-access`。

### 已知边界

- dsh SDK 没有协议级 prompt cancel，取消任务会终止该 workspace 的 runtime
  并重启。
- dsh SDK 不能跨进程恢复 session；`continuous` 模式只在同一 runtime 进程内
  复用上下文，worker 重启后会新建 session。
- dsh runtime 当前只支持一个 workspace per process；AgentSociety 会按
  workspace 维护独立 runtime。
- Hub steer/follow-up 不适用于 dsh 执行端（dsh 无对应控制原语）。

## 环境变量

见 [`.env.agent.example`](../.env.agent.example) 中 DeepSeek Harness 小节。
