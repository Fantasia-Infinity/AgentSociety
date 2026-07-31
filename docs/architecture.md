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
                                     │  远程 API（当前）         │
                                     │  本地 LLM（未来）         │
                                     └──────────────────────────┘
```

## 为什么采用事件与动作队列

模型推理不应阻塞微信的消息接收线程。Gateway 上报事件后立即得到接受结果，再通过
长轮询取得发送动作。未来本地模型变慢、服务器短暂断线或增加多账号时，微信侧仍可
持续收消息。

当前队列和 Outbox 在内存中，适合单实例开发。服务器化时保持 HTTP 协议不变，把它们
替换为 Redis Streams 或其他持久队列即可。

## 模型抽象

业务层只依赖 `ModelProvider.complete(ModelRequest) -> ModelResponse`。当前
`OpenAICompatibleProvider` 调用 `/chat/completions`。未来接入 Ollama、vLLM、
llama.cpp 或其他本地推理服务时有两条路径：

1. 推理服务兼容 OpenAI API：只修改 `LLM_BASE_URL`、`LLM_MODEL` 和密钥。
2. 协议不兼容：新增一个 `ModelProvider` 实现，Bot Core 无需改变。

模型能力（工具调用、视觉、结构化输出、上下文长度）以后应作为显式配置和启动探针，
不能通过模型名称猜测。

## 当前状态与后续替换点

| 能力 | 当前实现 | 服务器化替换 |
| --- | --- | --- |
| 模型 | 远程 OpenAI-compatible API | 本地推理服务 |
| 任务队列 | 进程内有界队列 | Redis Streams |
| 回复 Outbox | 进程内、按账号长轮询 | Redis/持久消息总线 |
| 会话历史 | 进程内存 | PostgreSQL |
| 微信接入 | 标准事件/动作协议 | Windows Gateway |
| 鉴权 | Bearer token | TLS + 设备身份/密钥轮换 |

## 安全默认值

- 私聊与群聊均使用显式 allowlist；空 allowlist 表示拒绝。
- 群聊默认只有 `mentioned_bot=true` 才会调用模型。
- 自己发送的消息不会再次进入 Bot，避免回复死循环。
- API Key 只从环境读取，不进入事件、回复或日志。
- 健康检查不包含密钥和聊天内容。

