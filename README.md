# WeChat Bot

一个本地优先的 Agent 协作平台及微信通信适配器。Windows Gateway 只负责操作微信客户端；
平台无关的 Core 负责消息与 Coordination Hub；每台开发设备可运行 Pi Agent Host，既允许
登录用户直接操作，也能领取其他设备委派的持久任务。所有 Agent 默认调用远程 API。

## 已实现

- `wechat-bot-core`：HTTP 接入、显式 allowlist、持久化收件箱、去重、会话、回复 Outbox
  和 LLM 调用。
- `wechat-gateway`：消息采集、历史游标与 SQLite Inbox、回复长轮询、租约与 ACK、本地发送账本。
- `mock` 适配器：可在 macOS 上用 JSON 行模拟微信消息，验证完整链路。
- `wxauto4` / `wxautox4` 适配边界：Windows 下动态加载，不会成为 Core 的依赖。
- `ModelProvider`：支持 `remote`、`local_rwkv` 和显式远程回退的 `auto` 模式。
- `agent_hub`：通用 Principal / Actor / Node / Task / Run / Artifact 模型、租约和事件流。
- `agent-host`：Pi SDK 本地交互与远程任务 worker；远程任务默认只读工具策略。

完整设计见 [架构文档](docs/architecture.md)，Windows 部署见
[Windows Gateway 指南](docs/windows-gateway.md)，Agent 平台见
[Pi Agent 协作平台](docs/agent-platform.md)，Mac 端侧推理见
[本地 RWKV 指南](docs/local-rwkv.md)。

## Pi Agent Host

Core 启动后，可安装 Pi Host（需要 Node.js 22.19 或更高版本）：

```bash
cd agent-host
npm ci --ignore-scripts
npm run apply-security-patches
npm run security-check
npm run build
npm run start -- register
npm run interactive  # 本机登录用户直接操作
npm run worker       # 领取 Hub 委派的任务
```

默认复用项目 `.env` 中的远程 LLM 与 Core token；详细身份、workspace、权限策略及 API 见
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
