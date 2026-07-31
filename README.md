# WeChat Bot Core

一个与微信接入方式、模型供应商解耦的最小 Bot Core。当前模型实现调用远程
OpenAI-compatible Chat Completions API；以后可把同一接口指向服务器上的本地
LLM，无需修改微信消息链路。

## 当前边界

- `wechat-gateway`（后续在原生 Windows 上实现）：登录微信、接收消息、轮询待发送动作。
- `wechat-bot-core`（本项目）：访问控制、去重、会话、任务队列、LLM 调用和回复生成。
- `ModelProvider`：模型抽象；当前实现为 `OpenAICompatibleProvider`。

完整设计见 [架构文档](docs/architecture.md)。

## 本地启动

项目只使用 Python 标准库，当前无需安装第三方依赖。

```bash
cp .env.example .env
# 编辑 .env，至少设置 BOT_API_TOKEN、LLM_BASE_URL、LLM_API_KEY、LLM_MODEL
PYTHONPATH=src python3 -m wechat_bot.api
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

模拟 Windows Gateway 上报一条私聊消息：

```bash
curl -X POST http://127.0.0.1:8080/v1/events/wechat \
  -H 'Authorization: Bearer replace-with-a-long-random-token' \
  -H 'Content-Type: application/json' \
  -d '{
    "message_id": "msg-1",
    "account_id": "account-1",
    "chat_id": "test-user-id",
    "sender_id": "test-user-id",
    "chat_type": "direct",
    "content_type": "text",
    "content": "你好",
    "timestamp": 1785500000
  }'
```

Gateway 使用长轮询取得回复动作：

```bash
curl 'http://127.0.0.1:8080/v1/actions?account_id=account-1&timeout=25' \
  -H 'Authorization: Bearer replace-with-a-long-random-token'
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用假的模型传输层，不会请求真实 LLM，也不需要 API Key。

