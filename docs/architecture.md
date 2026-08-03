# 架构

## 目标拓扑

```text
Windows 微信设备                       平台无关的 Bot/模型服务器
┌──────────────────┐                 ┌──────────────────────────┐
│ 微信客户端         │  HTTPS/WSS      │ Bot Core                 │
│ WeChat Gateway   │ ──────────────▶ │ 访问控制 / 去重 / 会话    │
│ 登录、收发、心跳   │ ◀────────────── │ 队列 / 回复动作           │
└──────────────────┘                 │          │               │
                                     │          ▼               │
                                     │     ModelProvider        │
                                     │          │               │
                                     │  远程 API（默认）         │
                                     │  本地 RWKV（可选）        │
                                     └──────────────────────────┘
```

这条微信链路现在是通用 Agent 平台的一个 Channel Adapter。独立部署的 Coordination Hub
管理 Principal、Actor、Node、Task、Run 和 Artifact；它不在微信 Core 进程中。各开发设备
上的 Pi Agent Host 同时提供本机用户入口和远程任务入口。详细模型、API 与演进边界见
[Pi Agent 协作平台](agent-platform.md)。

## 运行边界

- Windows 设备只运行微信客户端与 `wechat-gateway`，不保存 LLM 密钥。
- macOS 或服务器可分别运行 `wechat-bot-core` 和 `agent-hub`；两者使用独立端口、状态库和
  Bearer token。
- 微信 Gateway 与 Core 只共享版本化消息协议；Agent Host 与 Hub 只共享协作任务协议。

## 为什么采用事件与动作队列

模型推理不应阻塞微信的消息接收线程。Gateway 上报事件后立即得到接受结果，再通过
长轮询取得发送动作。未来本地模型变慢、服务器短暂断线或增加多账号时，微信侧仍可
持续收消息。

Gateway 和 Core 现在都采用本地 SQLite 持久化：Gateway 先把消息写入 Inbox，再上传
Core；模型返回后，Core 在单个事务中写入会话、去重完成、回复 Outbox 和 Inbox 完成。
处理租约会在长推理期间续期，进程崩溃只能留下“全部提交”或“全部重试”。服务器化时
保持 HTTP 协议不变，可把两端 SQLite 分别替换为 Redis Streams、PostgreSQL 或其他持久
队列/数据库。

## 回复投递语义

1. Gateway 长轮询 `/v1/actions`，Core 为动作设置租约但不删除。
2. Gateway 调用微信发送接口；成功后先把 `action_id` 写入本机 SQLite。
3. Gateway 调用 `/v1/actions/ack`；Core 收到 ACK 后删除动作。
4. 若发送成功但 ACK 丢失，租约到期后动作重新出现；SQLite 账本使 Gateway 只重发 ACK，
   不重复操作微信。

这提供进程重启后仍有效的近似 exactly-once 发送。发送成功与 Windows SQLite 落盘之间仍有
一个很小的崩溃窗口，无法在第三方 UI 操作与本地事务之间做到严格原子性。Core Outbox 已经
持久化到 SQLite；服务器化时可以继续使用，或按负载替换为持久消息总线。

微信消息回调只做轻量标准化和本地 SQLite 入队，不在回调里发送或访问网络。Core 下线时，
Gateway 会保留待上传 Inbox 并持续重试；Gateway 正常重启后会从 Inbox 继续上传。异常断电
时依靠租约恢复，仍应通过实机故障测试验证极端崩溃窗口。

## 模型抽象

业务层只依赖 `ModelProvider.complete(ModelRequest) -> ModelResponse`。
`OpenAICompatibleProvider` 调用 `/chat/completions`，可以连接远程 API 或本机
llama.cpp。`LLM_BACKEND` 支持三种路由：

1. `remote`：只调用远程 API，也是向后兼容的默认值。
2. `local_rwkv`：只调用本机推理服务；故障时由持久化 Inbox 退避重试。
3. `auto`：本地失败后调用远程；这是会改变数据边界的显式选择。

接入 Ollama、vLLM、llama.cpp 或其他推理服务时仍有两条路径：

1. 推理服务兼容 OpenAI API：只修改 `LLM_BASE_URL`、`LLM_MODEL` 和密钥。
2. 协议不兼容：新增一个 `ModelProvider` 实现，Bot Core 无需改变。

模型能力（工具调用、视觉、结构化输出、上下文长度）以后应作为显式配置和启动探针，
不能通过模型名称猜测。

## 当前状态与后续替换点

| 能力 | 当前实现 | 服务器化替换 |
| --- | --- | --- |
| 模型 | 远程或本机 OpenAI-compatible API | 独立推理服务器 |
| Gateway Inbox | Windows SQLite，消息状态 + 聊天游标 | Redis Streams/数据库 |
| Core 任务队列 | Mac SQLite，消息状态 + 租约 | Redis Streams |
| 回复 Outbox | Mac SQLite，租约 + ACK | Redis/持久消息总线 |
| 会话历史 | Mac SQLite | PostgreSQL |
| 微信接入 | Windows Gateway + mock/wxauto 适配器 | 其他合规渠道适配器 |
| Gateway 消息与发送账本 | 本地 SQLite | SQLite 可继续使用 |
| Agent 运行时 | Pi SDK Agent Host | Pi、其他框架及 A2A Adapter 并存 |
| Agent 工具 | 托管 Sub-agent/plan/memory/background + Pi LSP/MCP + Channel MCP | 更多 MCP 客户端与节点策略 |
| 协作任务 | SQLite 或可选 PostgreSQL；Artifact URI 或 file/S3 对象存储 | 事件总线与 Hub 联邦 |
| 鉴权 | Bearer token | TLS + Principal/Node 身份与密钥轮换 |

## 安全默认值

- 私聊与群聊均使用显式 allowlist；空 allowlist 表示拒绝。
- 群聊默认只有 `mentioned_bot=true` 才会调用模型。
- 自己发送的消息不会再次进入 Bot，避免回复死循环。
- API Key 只从环境读取，不进入事件、回复或日志。
- 健康检查不包含密钥和聊天内容。
- 本地模型启动器只允许绑定 loopback，避免无意暴露到局域网。
- 模型 HTTP 错误正文不会写入日志或持久化重试数据库，防止服务回显提示词。
- Gateway 只支持配置中列出的监听会话，不主动遍历或群发联系人。
- Agent Hub 默认只绑定 loopback；远程节点通过带 TLS 的反向代理访问并使用独立 token。
