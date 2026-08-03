# AgentSociety

一个本地优先、可独立使用也可组成协作网络的 Agent 平台。每台设备上的 Agent 默认就是普通
Pi Agent，通过本机 TUI 由登录用户直接操作；配置 Hub 后才增加跨设备任务、Run、Artifact
和 session 观察能力。微信只是可选通信适配器：Windows Gateway 负责客户端操作，微信 Core
负责消息、回复和模型调用。所有 Agent 默认调用远程 API。

## 已实现

- `wechat-bot-core`：HTTP 接入、显式 allowlist、持久化收件箱、去重、会话、回复 Outbox
  和 LLM 调用；模型结果的历史、去重、Outbox、Inbox 完成使用同一个事务提交。
- `wechat-gateway`：消息采集、历史游标与 SQLite Inbox、回复长轮询、租约与 ACK、本地发送账本。
- `mock` 适配器：可在 macOS 上用 JSON 行模拟微信消息，验证完整链路。
- `wxauto4` / `wxautox4` 适配边界：Windows 下动态加载，不会成为 Core 的依赖。
- `ModelProvider`：支持 `remote`、`local_rwkv` 和显式远程回退的 `auto` 模式。
- `agent-hub`：独立的 Principal / Actor / Node / Task / Run / Artifact 服务、租约和事件流。
- `agent-host`：Pi SDK 原生 TUI 与远程任务 worker；本地和远程默认具备 Sub-agent、
  plan/todo、分域长期记忆、LSP/代码索引、MCP 和 session 后台进程，并持久化每次任务的
  Pi session。
- `web_search`：模型无关的 Pi 工具契约；当前 adapter 使用 DeepSeek Responses API 的
  服务端搜索，并返回答案及可用的来源 URL。
- `agent-channel-mcp`：MCP 2025-06-18 stdio server，统一 list/read/send/reply/react/download
  通道工具；微信适配器当前实现前四项并显式声明其余能力不可用。
- A2A 1.0 JSON-RPC Adapter：标准 Agent Card 和 Send/Get/List/Cancel Task 映射。

完整设计见 [架构文档](docs/architecture.md)，Windows 部署见
[Windows Gateway 指南](docs/windows-gateway.md)，Agent 平台见
[Pi Agent 协作平台](docs/agent-platform.md)，Mac 端侧推理见
[本地 RWKV 指南](docs/local-rwkv.md)。

## 独立 Agent Hub

Hub 和微信 Core 不共享进程、端口、SQLite 或 token：

```bash
cp .env.hub.example .env.hub
# 设置一个至少 24 字符的独立 AGENT_HUB_TOKEN
PYTHONPATH=src python3 -m agent_hub.server
```

它默认只监听 `127.0.0.1:8090`。公网服务器部署模板见
[`deploy/hub`](deploy/hub) 和 [Agent 平台文档](docs/agent-platform.md)。

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
```

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
PYTHONPATH=src python3 -m wechat_bot.api
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
PYTHONPATH=src python3 -m wechat_bot.local_model
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

测试覆盖 Core、HTTP 协议解析、Gateway、ACK 丢失去重和 wxauto 消息标准化；使用假的
模型与微信对象，不会请求真实 LLM，也不需要 API Key。
