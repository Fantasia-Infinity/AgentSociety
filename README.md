# AgentSociety

一个本地优先、可独立使用也可组成协作网络的 Agent 平台。每台设备上的 Agent 默认就是普通
Pi Agent，通过本机 TUI 由登录用户直接操作；配置 Hub 后才增加跨设备任务、Run、Artifact
和 session 观察能力。微信只是可选通信适配器：Windows Gateway 负责客户端操作，微信 Core
负责消息、回复和模型调用。所有 Agent 默认调用远程 API。

## 已实现

- `wechat-core`：HTTP 接入、显式 allowlist、持久化收件箱、去重、会话、回复 Outbox
  和 LLM 调用；模型结果的历史、去重、Outbox、Inbox 完成使用同一个事务提交。
- `wechat-gateway`：消息采集、历史游标与 SQLite Inbox、回复长轮询、租约与 ACK、本地发送账本。
- `mock` 适配器：可在 macOS 上用 JSON 行模拟微信消息，验证完整链路。
- `wxauto4` / `wxautox4` 适配边界：Windows 下动态加载，不会成为 Core 的依赖。
- `ModelProvider`：支持 `remote`、`local_rwkv` 和显式远程回退的 `auto` 模式。
- `agent-hub`：独立的 Principal / Actor / Node / Task / Run / Artifact 服务、租约和事件流；
  支持多租户（token/OIDC）、PostgreSQL/S3 可选存储，以及 REST、MCP、A2A、Web 四个等价入口。
- `agent-host`：Pi SDK 原生 TUI 与远程任务 worker；本地和远程默认具备 Sub-agent、
  plan/todo、分域长期记忆、LSP/代码索引、MCP 和 session 后台进程。远程 worker 可选择
  每任务独立 Pi session，或按 principal/workspace/worker slot 复用可跨重启恢复的连续 session。
- 通用 Bridge：`./agent bridge --adapter codex|opencode|generic` 让任意带非交互 CLI 的
  agent 成为 Hub worker；codex/opencode 支持跨任务连续会话，任务信封与结果契约见
  [agent-adapters.md](docs/agent-adapters.md)。
- `web_search`：模型无关的 Pi 工具契约；当前 adapter 使用 DeepSeek Responses API 的
  服务端搜索，并返回答案及可用的来源 URL。
- `agent-channel-mcp`：MCP 2025-06-18 stdio server，统一 list/read/send/reply/react/download
  通道工具；微信适配器当前实现前四项并显式声明其余能力不可用。
- A2A 1.0 JSON-RPC Adapter：标准 Agent Card 和 Send/Get/List/Cancel Task 映射。
- Hub MCP Server：`/mcp` 暴露 `hub_create_task` / `hub_get_task` / `hub_cancel_task` 等
  工具，Codex/OpenCode/Claude 可直接作为派发端；stdio 与 streamable HTTP 两种传输。
- Hub Web 管理界面：管理员/租户仪表盘（任务、Run、Artifact、节点、租户、token），
  会话 Cookie + CSRF，可选 OIDC 登录。

完整设计见 [架构文档](docs/architecture.md)，Windows 部署见
[Windows Gateway 指南](docs/windows-gateway.md)，Agent 平台见
[Pi Agent 协作平台](docs/agent-platform.md)，Mac 端侧推理见
[本地 RWKV 指南](docs/local-rwkv.md)。

公网部署 Hub（无域名/域名、PostgreSQL/S3、Web 管理界面、多租户）见
[Hub 公网部署指南](docs/public-hub.md)。
通用 agent 适配器（codex/opencode/generic、任务信封、连续会话、GUI 可见性）见
[agent-adapters.md](docs/agent-adapters.md)。

## 独立 Agent Hub

Hub 和微信 Core 不共享进程、端口、SQLite 或 token：

```bash
cp .env.hub.example .env.hub
# 设置一个至少 24 字符的独立 AGENT_HUB_TOKEN
PYTHONPATH=src python3 -m agent_hub.server
```

它默认只监听 `127.0.0.1:8090`。公网服务器部署模板见
[`deploy/hub`](deploy/hub) 和 [Agent 平台文档](docs/agent-platform.md)。

Hub 提供四个等价入口，共享同一内核（`AgentHubApi` → Store），状态天然一致：

| 入口 | 路径 | 用途 |
|---|---|---|
| REST | `/v1/hub/*` | 全量 API，Pi worker/bridge 的 HubClient 使用 |
| MCP | `/mcp` | Codex/OpenCode/Claude 等 MCP 客户端派发/观察任务 |
| A2A | `/a2a` | 标准 Agent Card + Send/Get/List/Cancel Task |
| Web | `/web` | 人类管理界面（需设置 `AGENT_HUB_WEB_SECRET`） |

版本策略（REST `/v1` 路径版本化、MCP `protocolVersion`、A2A `A2A-Version` 头）见
[架构文档](docs/architecture.md)。

## Pi Agent Host

新设备只需要 Git、Node.js 22.19+ 和远程模型的三项连接信息。clone 后直接运行：

```bash
git clone <repository-url> AgentSociety
cd AgentSociety
./agent
```

Windows PowerShell 使用 `./agent.ps1`。首次运行只要求 OpenAI-compatible 模型 URL、model
ID 和 API key；Hub URL/token 是可留空的附加项。随后自动安装锁定依赖、应用安全补丁、构建、
生成本机身份、使用仓库根目录作为 workspace、调用一次最小模型连通性检查，并打开 Pi 原生
TUI。配置 Hub 时才会额外登记 Principal/Actor/Node。
非敏感配置保存在被 Git 忽略且权限为 `0600` 的 `.env.agent`；LLM API key 和 Hub token
只保存到 macOS Keychain、Windows Credential Manager 或 Linux Secret Service，配置文件
不含明文凭据。系统安全凭据库不可用时 setup 会停止，不会降级写入文件。

安装完成后，入口会自动在用户级 `~/.local/bin/agent` 注册全局命令；只要该目录在
`PATH` 中，之后可从任意目录直接运行 `agent`。已有同名非 AgentSociety 命令时，安装器
会拒绝覆盖；可用 `AGENT_GLOBAL_BIN=/path/to/bin` 指定其他用户级目录。

后续常用命令：

```bash
./agent                 # 打开本机 Pi TUI
./agent local           # 即使已配置 Hub，也强制以普通 Agent 模式启动
./agent worker          # 持续领取 Hub 任务
./agent doctor          # 复查 Hub、workspace、session 和远端模型
./agent sessions        # 离线列出本机 session
./agent observe task_xxx
./agent control task_xxx  # 交互式 steer/follow-up/status/cancel
./agent steer task_xxx "立即先运行单元测试"
./agent follow-up task_xxx "完成后再给出性能摘要"
./agent cancel task_xxx "目标已变化"
./agent bridge --adapter codex   # 用 Codex CLI 作为持续 worker
./agent bridge --adapter opencode # 用 OpenCode 作为持续 worker
./agent bridge --adapter generic  # 自定义命令适配器
```

远程任务默认使用 `AGENT_WORKER_SESSION_MODE=per_task`，每个 Task 都有独立 Pi session。
改成 `continuous` 后，同一个 principal、workspace 和 worker slot 的顺序任务会复用同一个
session，从而保留模型上下文；映射在首次模型回复后落盘，worker 重启后自动恢复。可用
`AGENT_WORKER_SESSION_MAX_TASKS` / `AGENT_WORKER_SESSION_MAX_AGE_HOURS` 自动轮换，或在单个
Task 的 `input` 中传 `reset_worker_session: true` 立即换新。每个 Run 仍单独审计，并记录
session ID、是否复用和该任务对应的 JSONL entry 边界。

本地 TUI 使用 Pi 原生资源加载器，兼容 Pi Package 中的 Extension 自定义工具、命令、事件、
Skill、Prompt 和 Theme。Package 仍用 Pi 自己的入口管理，不额外包装安装命令：

```bash
npm --prefix agent-host exec -- pi install npm:<package-name>
npm --prefix agent-host exec -- pi list
```

项目内 `.pi` 资源首次加载前会要求信任；Hub worker 默认不执行 Pi Package。远程插件策略见
[Pi Agent 协作平台](docs/agent-platform.md)。第三方 Extension 与普通本地程序权限相同，安装前
应审查来源和代码。

AgentSociety 固定并加载 `pi-mcp-adapter` 与 `pi-lsp-adapter`，新安装会自动为 Channel MCP
写入一个不含 secret 的托管配置，并让 LSP 在首次使用某种语言时自动安装固定版本的语言服务。
其他 Pi MCP 配置和社区 Package 仍按 Pi 原生方式工作。Hub custom tools 继续直接注入 Pi，
二者共享领域契约而不要求统一插件安装入口。

内建能力对本地 TUI 和 `full`（默认）远程 worker 使用同一实现：`subagent` 创建隔离子 session；
plan/todo 按 session 持久化；memory 分 workspace 与 principal 保存且不会记录模型凭据；后台进程
只属于当前 session，session 结束会清理。`AGENT_BUILTIN_CAPABILITIES=0` 可整体关闭；显式的
`read_only`/`no_tools` 远程策略仍优先限制可执行工具。

当模型地址是 `https://api.deepseek.com` 时，`AGENT_WEB_SEARCH=auto`（默认）会复用系统
凭据库中的模型 key，并用 `deepseek-v4-flash` 调用 Responses API 的服务端 `web_search`。
普通 Chat Completions 仍负责主 session；搜索作为独立、可替换的工具调用返回结果。可设置
`AGENT_WEB_SEARCH=disabled` 完全关闭，或设置 `deepseek` 为兼容代理强制启用。当前 DeepSeek
adapter 只接收通用 `query`，将来可在不改变 Pi 工具名的情况下替换其他搜索 provider。返回值以
`citationsProvided` 明示提供商是否给出了结构化 URL 引用；未提供时不会把搜索动作 URL 冒充引用。

高级身份覆盖、workspace、权限策略、远端 session 查看方式及 API 见
[Pi Agent 协作平台](docs/agent-platform.md)。

## 在 Mac 上启动 Bot Core

项目本身只使用 Python 标准库。

```bash
cp .env.example .env
# 默认 LLM_BACKEND=remote；至少设置 BOT_API_TOKEN、LLM_BASE_URL、LLM_MODEL
PYTHONPATH=src python3 -m wechat_core.api
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

另开一个终端启动模拟 Gateway（两个配置中的 `BOT_API_TOKEN` 必须相同）：

```bash
cp .env.gateway.example .env.gateway
# 保持 WECHAT_DRIVER=mock，并编辑连接信息
PYTHONPATH=src python3 -m wechat_gateway
```

在 Gateway 终端输入一行：

```bash
{"chat_id":"test-user-id","content":"你好"}
```

模型回复会以 `BOT_REPLY {...}` 输出。`test-user-id` 也必须存在于 Core 的
`BOT_ALLOWED_USERS` 中。

## 可选的 Mac 本地 RWKV

本地模式使用单独的 `llama-server` 进程监听 `127.0.0.1:18080`，Core 继续监听
`8080`。当前远程配置无需删除；把 `LLM_BACKEND` 改成 `local_rwkv` 才会启用本地
模型，把它改成 `auto` 才会在本地失败后向远程发送同一会话。

```bash
# 安装 llama.cpp 并下载兼容的 RWKV-6/7 GGUF 后：
PYTHONPATH=src python3 -m wechat_core.local_model
```

模型文件、采样配置、健康检查和 LaunchAgent 模板参见
[本地 RWKV 指南](docs/local-rwkv.md)。

## Windows 微信接入

建议使用原生 Windows 10/11 或 Windows Server，Python 3.12。Gateway 当前依据 wxauto
4.x 接口实现；官方文档目前把 `AddListenChat` 标记为 Plus 能力，因此新安装首选
`wxautox4`，`wxauto4` 仅保留兼容入口。微信和 wxauto 小版本必须匹配，详见
[wxauto 安装兼容表](https://docs.wxauto.org/docs/install.html)。

此类 UI Automation 接入不是微信官方 Bot API，有账号风控、客户端升级失效及使用条款
风险。wxauto 的协议限定合法的个人学习/研究用途并禁止商业用途；使用前请阅读
[wxauto 用户协议](https://docs.wxauto.org/agreement.html)和微信相关条款。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试按子系统组织在 `tests/hub/`（REST/MCP/A2A/Web、多租户、Web 会话）与
`tests/wechat/`（Core、HTTP 协议解析、Gateway、ACK 丢失去重、wxauto 消息标准化）；
使用假的模型与微信对象，不会请求真实 LLM，也不需要 API Key。
