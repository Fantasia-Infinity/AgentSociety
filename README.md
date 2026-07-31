# WeChat Bot

一个面向个人微信号的分离式 Bot：Windows Gateway 负责操作微信客户端，平台无关的
Bot Core 负责访问控制、会话和 LLM。当前模型调用远程 OpenAI-compatible Chat
Completions API；以后把同一接口指向本地 LLM 即可，无需改微信消息链路。

## 已实现

- `wechat-bot-core`：HTTP 接入、显式 allowlist、去重、会话、任务队列和 LLM 调用。
- `wechat-gateway`：消息上报、回复长轮询、租约与 ACK、本地 SQLite 发送账本。
- `mock` 适配器：可在 macOS 上用 JSON 行模拟微信消息，验证完整链路。
- `wxauto4` / `wxautox4` 适配边界：Windows 下动态加载，不会成为 Core 的依赖。
- `ModelProvider`：当前为远程 OpenAI-compatible API，已为本地模型保留替换点。

完整设计见 [架构文档](docs/architecture.md)，Windows 部署见
[Windows Gateway 指南](docs/windows-gateway.md)。

## 在 Mac 上启动 Bot Core

项目本身只使用 Python 标准库。

```bash
cp .env.example .env
# 编辑 .env，至少设置 BOT_API_TOKEN、LLM_BASE_URL、LLM_API_KEY、LLM_MODEL
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
