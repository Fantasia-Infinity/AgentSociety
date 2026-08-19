[English](README.en.md) | [中文](README.md)

# AgentSociety —— 你的赛博同事网络

AgentSociety 是一个**跨设备、多 Agent 协作框架**：把家里的 Mac、办公室的
Windows、云上的服务器，以及任何带命令行工具的机器，连成一张“同事网络”。
每一台设备上的 Agent 都是你的一名赛博同事——你可以从任何一端派活、看进度、
收结果，也可以让不同设备上的 Agent 互相协作。

默认 Agent 运行时是 **DeepSeek Harness（dsh）插件**（`dsh-plugin/`）：
AgentSociety 作为 dsh 进程内 bundle 加载，`./agent` 打开 dsh-TUI，
`./agent worker` 启动 dsh 进程内 Hub worker；Pi 作为兼容回退继续保留。

```mermaid
flowchart LR
    You[你 / Codex / Web 仪表盘] --> Hub((Hub 协调中枢))
    Hub --> Mac[Mac Agent]
    Hub --> Win[Windows Agent]
    Hub --> Server[服务器 Agent]
    Hub --> Any[任意 CLI Agent<br/>codex / opencode / generic]
```

## 它解决什么问题

- 你在一台机器上想到一个任务，想让另一台机器上的 Agent 去执行——不用手动 SSH
  或复制文件，在 Hub 上派发即可。
- 你想让多台设备上的 Agent 各自负责一块工作（跑测试、查资料、操作微信、整理
  文件），最后把结果汇总给你。
- 你想用 Codex、OpenCode 或任意命令行 Agent 作为执行端，而不是重新学一套新工具。

AgentSociety 只负责**协调**：谁是谁、谁在跑什么、跑到哪一步、结果放在哪里。
真正的“干活”始终发生在你自己的设备上，由你自己安装的 Agent 完成。

### 最近更新：远程访问设备上的 DSH Web

AgentSociety 现在可以把每台设备上的本地 DSH Web 安全地挂载到 Hub，浏览器无需把设备端口暴露到公网：

- `agent web-bridge` 通过设备主动出站的受控 WebSocket 隧道连接 Hub；NAT 和防火墙后的设备也可以接入。
- 浏览器使用 `/v1/web/<node_id>/` 访问对应设备，Hub 只做认证、授权和透明转发，不改写 HTML，也不注入全局运行时补丁。
- DSH Web 前端使用原生相对路径，API、插件、静态资源、manifest、favicon、HMR 和 `events.mux` / `events.host` WebSocket 都能在节点挂载路径下工作。
- bridge 默认会自动启动本机 `agent-society-web` profile；如果已经运行 `agent web`，则复用已有实例，退出时只回收自己启动的子进程。
- bridge 启动时会幂等创建默认 workspace（默认是当前用户 home），并在 Web 页面加载后自动进入该 workspace。

快速启动：

```bash
./agent web-bridge
# 默认本地 DSH Web: http://127.0.0.1:3080
# 远程访问: https://<hub>/v1/web/<node_id>/
```

常用环境变量：

```bash
AGENT_DSH_WEB_TARGET=http://127.0.0.1:3080
AGENT_DSH_WEB_DEFAULT_WORKSPACE=/path/to/workspace
AGENT_DSH_WEB_BRIDGE_START=0  # 仅使用外部管理的 DSH Web，不自动启动
```

同一个 `node_id` 只应运行一个 bridge；多个 bridge 会互相替换隧道并反复重连。完整协议、路径白名单和安全边界见 [DSH Web Hub Bridge](docs/dsh-web-hub-bridge.md)。

真正的“干活”始终发生在你自己的设备上，由你自己安装的 Agent 完成。

## 核心概念

| 概念 | 含义 |
|---|---|
| Hub | 协调中枢，保存身份、任务、运行记录和事件流 |
| Principal | 一个人/组织（比如你），是数据隔离的单位 |
| Actor | 一个 Agent 的身份（比如 `pi-我的mac`） |
| Node | 一台设备（比如你的 Mac 或服务器） |
| Task | 一次委派的目标和指令，可指定给某个 Actor 或按能力匹配 |
| Run | 一次实际执行（Task 可能因失败/取消被多次执行） |
| Event | 任务事件流（提交、认领、开始、完成…），全程可审计 |
| Artifact | 任务产出的文件或对象 |

身份关系：**Principal（你）→ Actor（Agent）→ Node（设备）**。每个用户只
能看见属于自己的数据，不同租户之间互相隔离。

## 快速开始

### 1. 准备一个 Hub

最简单的办法是把 Hub 部署在一台有公网 IP 的服务器上（正式部署建议配一个域名），
也可以先跑在局域网机器上体验。部署步骤见
[Hub 公网部署指南](docs/public-hub.md)：

```bash
cp .env.hub.example .env.hub
# 设置 AGENT_HUB_TOKEN（至少 24 字符）和 AGENT_HUB_WEB_SECRET（至少 32 字符）
PYTHONPATH=src python3 -m agent_hub.server
```

启动后打开 `http://<hub-address>:8090/web` 注册你的账号。Hub 默认只监听
`127.0.0.1`，公网访问通过 Caddy/Cloudflare 等反向代理暴露。

### 2. 在一台设备上安装 Agent

推荐使用 [dsh-agent-society-combo](https://github.com/Fantasia-Infinity/dsh-agent-society-combo)：
它把 DeepSeek Harness、dsh-TUI、AgentSociety 和默认 `anchored-standard` preset 固定到
一组验证过的 commit，并打上兼容补丁：

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/Fantasia-Infinity/dsh-agent-society-combo/main/install.sh | bash

# Windows PowerShell
# irm https://raw.githubusercontent.com/Fantasia-Infinity/dsh-agent-society-combo/main/install.ps1 | iex
```

安装完成后，`agent` 默认启动带 AgentSociety Hub 工具的 dsh-TUI，`agent web`
启动带同一插件的 dsh Web UI，`agent worker` 默认启动 dsh 进程内 worker；
TUI / Web / worker 共享 `~/.dsh` 里的 session 与插件配置。缺少 dsh-TUI /
DeepSeek Harness 时 TUI 和 worker 自动回退 Pi。

源码开发方式仍支持：

```bash
git clone <repository-url> AgentSociety
cd AgentSociety
sh scripts/install-dsh-plugin.sh
./agent               # dsh TUI；Windows 用 ./agent.ps1
./agent web           # dsh Web UI（同一 ~/.dsh session）
```

首次运行会引导你填写模型连接信息（OpenAI-compatible URL、Model ID、API Key），
然后自动安装依赖、构建、生成本机身份并打开 TUI。Hub 连接是可选的，之后随时
补充：

```bash
./agent setup         # 重新配置，可补填 Hub URL / 用户名 / 密码
./agent connect       # 用 Hub 账号密码换本机节点凭据
./agent web           # 浏览器 UI（--port 可改端口）
./agent worker        # 作为常驻 worker 开始领取 Hub 任务
./agent doctor        # 体检：Hub 连接、workspace、模型、session
```

非敏感配置保存在被 Git 忽略的 `.private/env/agent.env`（权限 0600）；API Key 和
Hub 凭据只存入系统凭据库（macOS Keychain / Windows Credential Manager / Linux
Secret Service），配置文件里没有明文密钥。

### 3. 派发你的第一个任务

配置好 Hub 后，从任何一端都可以派活：

- **Web 仪表盘**：`/web` 登录后创建任务、查看进度和结果。
- **Codex / OpenCode / Claude**：Hub 暴露 MCP 工具（`hub_create_task`、
  `hub_get_task`、`hub_cancel_task` 等），配置后即可直接在对话里派活。
- **本机 TUI / Web UI**：`./agent` 或 `./agent web`，通过 Hub 工具在对话里派发。
- **REST API**：`/v1/hub/tasks`，适合脚本和自动化。

用 Codex 连接 Hub 的 MCP（推荐走系统钥匙串，先运行 `agent connect` 保存节点凭据）：

```toml
[mcp_servers.hub]
command = "node"
args = ["/path/to/AgentSociety/agent-host/scripts/mcp-hub-wrapper.mjs"]
enabled = true
```

也可以直接用 URL + 节点凭据直连：`codex mcp add hub --url https://hub.example.com/mcp
--header "Authorization: Bearer <你的节点凭据>"`（凭据会写在 Codex 配置里，不推荐）。

### 4. 观察和干预

```bash
./agent sessions                  # 本机有哪些 session
./agent observe task_xxx          # 跟踪一个任务的实时进度
./agent control task_xxx          # 交互式 steer / follow-up / status / cancel
./agent steer task_xxx "先运行单元测试"
./agent cancel task_xxx "目标已变化"
```

## 支持的 Agent 类型

AgentSociety 不要求所有设备都用同一种 Agent：

- **DeepSeek Harness dsh 插件（默认）**：`dsh-plugin/`
  （`@agent-society/dsh-agent-society`）是当前主执行路径。它在 dsh 进程内
  提供 Hub worker（claim / heartbeat / controls / cancel / self-update）、
  `ctx.agents.create/resume` 连续会话、工具策略映射、session 标题与
  transcript artifact，并把 Hub 暴露为 `mcp__agent-society__hub_*` 工具。
  `./agent` 默认打开带该插件的 dsh-TUI，`./agent web` 打开同一插件的
  dsh Web UI，`./agent worker` 默认启动 dsh plugin worker。TUI 与 Web
  只是两个 UI adapter，共享 dsh core / AgentSociety 插件 / `~/.dsh` session；
  详见 [DeepSeek Harness 集成](docs/deepseek-harness.md)。
- **Pi Agent（保留兼容）**：完整的内建工具（Sub-agent、plan/todo、长期记忆、
  LSP、MCP、后台进程、Web 搜索）。`AGENT_TUI_RUNTIME=pi` /
  `AGENT_WORKER_RUNTIME=pi` 可强制回退，代码与 Pi 会话存储继续保留。
- **Codex / OpenCode**：通过通用 Bridge 作为 Hub worker
  （`./agent bridge --adapter codex`），支持跨任务连续会话，任务会出现在
  Codex GUI 的 “AgentHub” 项目里。
- **Generic**：任何带非交互 CLI 的工具都可以通过
  [适配器规范](docs/agent-adapters.md) 接入。

远程任务默认每次新建独立 session；改成 `continuous` 后，同一台设备同一工作区
的连续任务会复用同一个 session，保留模型上下文，worker 重启后还能恢复：

```dotenv
AGENT_WORKER_SESSION_MODE=continuous
```

## 四种访问入口，一个内核

| 入口 | 路径 | 用途 |
|---|---|---|
| REST | `/v1/hub/*` | 全量 API，脚本和 worker 使用 |
| MCP | `/mcp` | Codex / OpenCode / Claude 等 MCP 客户端 |
| A2A | `/a2a` | 标准 Agent Card 互操作 |
| Web | `/web` | 人类管理界面（注册、登录、任务、节点、账户） |

四个入口共享同一套任务/事件/租户状态，不存在“接口之间不一致”的问题；能力面
不同（REST 全量，MCP/A2A 是子集），新增能力优先扩展 REST。

## 安全设计

- **账号密码**：用户注册后用 argon2 哈希密码登录，Web 会话短期有效、可吊销。
- **节点凭据**：每台设备用 `agent connect` 换取独立、可单独吊销的节点凭据，
  不再使用共享 token。
- **数据隔离**：用户只能看到自己的 Principal / Actor / Node / Task / Run。
- **权限策略**：远程任务可设为 `read_only` / `no_tools`；dsh plugin worker
  按任务映射 `full` / `read_only` / `no_tools` 工具目录与 sandbox 模式，Pi
  插件资源默认不在 worker 中执行。详见 [认证文档](docs/authentication.md) 和
  [Agent 平台文档](docs/agent-platform.md)。

### 仅派发模式（dispatch-only）

如果某台设备只负责给其他设备派发任务、自己从不接收任务，可以设置
`AGENT_HUB_RECEIVE_DISABLED=1`：此时手动运行 `agent worker` 或
`agent bridge` 会被拒绝启动。派发能力（Web / REST / MCP）不受影响。

## 可选接入：微信通道（实验性，开发中）

微信不是 AgentSociety 的核心，而是**一个正在开发的可选通信工具**：在 Windows 上运行
`wechatd` 常驻服务（连接微信客户端，处理重登录与历史回补），Agent 通过 Channel MCP
工具直接收发微信消息。它依赖 wxauto 这类非官方 UI 自动化库，有客户端升级失效和账号
风控风险，目前建议只做个人学习/研究用途。想了解细节和风险，见
[Windows 微信守护进程指南](docs/windows-wechatd.md)。

其他可选组件：本地 RWKV 推理（[指南](docs/local-rwkv.md)）、Channel MCP 通道适配。

## 目录结构

```text
src/agent_hub/       Hub 协调中枢（REST/MCP/A2A/Web、存储、认证）
src/wechatd/         Windows 微信守护进程（本地 HTTP API，可选）
src/wechat_core/     微信 Core（已弃用，仅作参考）
src/agent_channel/   面向 Agent 的 Channel MCP 工具
dsh-plugin/          默认 Agent 运行时：DeepSeek Harness 进程内 bundle
agent-host/          Agent 宿主 CLI、Pi worker（回退）、Bridge
deploy/              Hub 的 Docker/Caddy 部署模板
docs/                架构、部署、适配器、认证文档
tests/               测试（Python + Node）
```

## 开发与测试

```bash
# Python（Hub / 微信）
PYTHONPATH=src python3 -m unittest discover -s tests -v

# Node（agent-host + dsh-plugin）
npm --prefix agent-host test
npm --prefix dsh-plugin run check
npm --prefix dsh-plugin run build
```

## 项目状态
- DSH Web 远程访问已可用：`agent web-bridge` 自动管理本地 Web、通过 Hub 原生挂载到 `/v1/web/<node_id>/`，并转发 RPC 与事件 WebSocket；详见 [DSH Web Hub Bridge](docs/dsh-web-hub-bridge.md)。

- 默认运行时：AgentSociety 作为 dsh 插件加载；`./agent` 打开 dsh-TUI，
  `./agent worker` 启动 dsh 进程内 worker；Pi 保留为兼容回退。
- 已可用：跨设备任务派发、连续会话、MCP/Web/REST 入口、账号与节点凭据、
  多租户隔离、Codex/OpenCode 适配器、dsh worker 的 steer/follow-up/cancel、
  工具策略、transcript artifact 和自更新；SSE 推送与长轮询领取、共享共识
  上下文、会话/Agent 信息目录（递进式查询）、任务中按需问答与委派
  （详见 [docs/shared-context.md](docs/shared-context.md)）。
- 开发中：发布与安装体验（[combo repo](https://github.com/Fantasia-Infinity/dsh-agent-society-combo)）、
  微信通道完善、更多 Agent 适配器、Web 租户自助管理、浏览器问题卡片
  （[docs/questions-web-card.md](docs/questions-web-card.md)）。

想深入了解，从 [架构文档](docs/architecture.md) 开始。

## License

MIT License，详见 [LICENSE](LICENSE)。
