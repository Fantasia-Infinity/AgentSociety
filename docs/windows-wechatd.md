# Windows 微信守护进程（wechatd）指南

## 形态

`wechatd` 是运行在原生 Windows 上的常驻服务：它持有微信客户端的 wxauto 连接，把
收发消息、重新登录恢复、历史回补和去重等状态全部留在本机，并通过仅绑定
`127.0.0.1` 的 HTTP API 暴露给本地 Agent。Agent 侧（Pi worker / codex / opencode）
通过 `agent_channel` MCP 工具收发微信，不需要知道 wxauto 的任何实现细节。

```
微信客户端（Windows，保持登录且窗口可交互）
   │ wxauto（免费轮询 / Plus 回调）
   ▼
wechatd（计划任务常驻，HTTP API 只绑 127.0.0.1）
   │ 127.0.0.1:8742
   ▼
agent_channel MCP（stdio，Agent 会话挂载）
   ▼
本地 Agent（channel_status / list_conversations / read_messages / send / reply）
```

当前 wxauto 4.x 官方环境是 Windows 10/11/Server 2016–2022。免费包当前限定 Python
3.9–3.12，Plus 支持范围更宽；本项目统一推荐 Python 3.12。客户端兼容版本更新较快，
安装前应以 [wxauto 官方安装页](https://docs.wxauto.org/docs/install.html)为准，不要让微信
客户端自动升级到尚未适配的版本。

## 安装

在 PowerShell 中：

```powershell
git clone https://github.com/Fantasia-Infinity/AgentSociety.git
cd AgentSociety
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

Plus 版可使用回调监听 `AddListenChat`：

```powershell
.\.venv\Scripts\python.exe -m pip install wxautox4
# 按 wxauto 官方说明激活 Plus
```

没有 Plus 激活码时可以安装免费版：

```powershell
.\.venv\Scripts\python.exe -m pip install wxauto4
```

免费包没有 `AddListenChat` 时，wechatd 会自动使用前台轮询。启动时先把当前可见消息记为
基线，只上报之后出现的新消息；轮询会逐个切换 `WECHAT_LISTEN_CHATS` 中的会话，因此建议
首轮只配置一个专用测试好友，并保持微信窗口可交互。

## 配置

```powershell
Copy-Item .env.wechatd.example .env.wechatd
notepad .env.wechatd
```

关键项示例：

```dotenv
WECHATD_ACCOUNT_ID=my-wechat-pc
WECHATD_HTTP_HOST=127.0.0.1
WECHATD_HTTP_PORT=8742
WECHATD_HTTP_TOKEN=可选的长随机值
WECHAT_DRIVER=wxautox4
WECHAT_LISTEN_CHATS=文件传输助手,家人群
WECHAT_BOT_MENTION=@你的微信昵称
WECHAT_POLL_INTERVAL_SECONDS=1
WECHATD_SEND_MIN_INTERVAL_SECONDS=1
```

- `WECHAT_LISTEN_CHATS` 是明确允许监听的好友备注名/群名，使用英文逗号分隔。
- wxauto 暴露的是界面显示名称而非稳定微信 ID，因此改备注、群名重名都会影响路由。
- 群聊默认只有文本中包含 `WECHAT_BOT_MENTION` 才会进入监听。
- `WECHAT_POLL_INTERVAL_SECONDS` 只影响没有回调接口的免费版，允许范围为 1–60 秒。
- `WECHATD_SEND_MIN_INTERVAL_SECONDS` 限制发送频率，降低账号风控风险。
- `WECHATD_HTTP_TOKEN` 设置后，所有 API 请求都必须带
  `Authorization: Bearer <token>`；Agent 侧用 `AGENT_CHANNEL_HTTP_TOKEN` 配置同一值。
- `WECHATD_STATE_DB` 保存消息归档、去重记录和恢复游标，不能放在会被定期清理的临时目录。

## 启动

先登录 Windows 微信并保持窗口可用，然后运行：

```powershell
.\.venv\Scripts\python.exe -m wechatd
```

验证服务：

```powershell
curl.exe http://127.0.0.1:8742/health
curl.exe http://127.0.0.1:8742/v1/status
```

### 长期运行

微信必须保持已登录且 Windows 用户会话保持可交互。可将 wechatd 注册为当前用户登录时
自动启动的计划任务；微信退出、wechatd 异常结束时会自动重试/拉起：

```powershell
.\scripts\install-wechatd-task.ps1
```

任务名为 `WechatdService`。运行日志写入 `wechatd-logs/`，不包含 HTTP token。查看、停止或
手动启动任务：

```powershell
Get-ScheduledTask -TaskName WechatdService
Stop-ScheduledTask -TaskName WechatdService
Start-ScheduledTask -TaskName WechatdService
```

wxauto 依赖可交互桌面。Windows 锁屏、RDP 断开后切换到不可交互会话、微信升级或窗口
结构变化都可能中断监听。官方的[云服务器部署说明](https://docs.wxauto.org/deploy.html)
也明确要求窗口保持活跃。

## HTTP API（v1，仅本地）

| 端点 | 说明 |
|---|---|
| `GET /health` | 健康检查，无需 token |
| `GET /v1/status` | 连接状态、监听会话、归档深度 |
| `GET /v1/chats?limit=` | 已归档会话列表 |
| `GET /v1/messages?chat_id=&limit=&after_message_id=&before_timestamp=` | 读取消息（游标增量或历史回看） |
| `GET /v1/message?message_id=` | 读取单条消息 |
| `POST /v1/send` | 发送文本 `{chat_id, content, chat_type?, idempotency_key?}` |
| `GET /v1/agent_cursor?chat_id=` | 读取 Agent 侧已读游标 |
| `PUT /v1/agent_cursor` | 更新 Agent 侧已读游标 `{chat_id, cursor}` |

## 重新登录与历史回补

- wechatd 会记住每个聊天最后处理到的消息游标（`chat_cursors`）与消息指纹去重记录；
  微信客户端退出后，免费版会按指数退避尝试重建 UI 连接。
- 重新登录后，wechatd 会用持久游标回补微信 UI 当前仍能加载的历史消息，不重复投递
  游标之前的内容。
- Agent 侧读取使用独立游标（`agent_cursors`）：首次读取返回归档中该会话的全部消息，
  之后每次读取只返回新消息并自动推进游标；携带 `before_timestamp` 的历史读取不推进
  游标。
- 若消息已经被微信客户端清理、尚未同步到 UI 或超出 UI 可加载范围，wxauto 无法保证获取。

## 首次测试清单

1. 先只监听“文件传输助手”或专用测试好友/群。
2. 启动 wechatd，确认 `GET /v1/status` 的 `adapter.connected` 为真。
3. 给测试会话发一条普通文本，确认 `GET /v1/messages?chat_id=...` 能读到该消息。
4. 用 `POST /v1/send` 回复，确认微信客户端实际发出。
5. 退出微信客户端后重新登录，确认历史消息回补且不重复。
6. 在 Agent 会话中确认 `channel_status`、`channel_read_messages`、`channel_send` 工具可用。
7. 再逐个增加监听会话，避免高频、群发或营销式自动操作。

## 已知限制

- 当前只发送文本；图片、语音、文件会被标准化，但 Agent 侧会收到不支持的内容类型。
- 免费版轮询启动时有约 3 秒预热，预热期间出现的消息会被当作基线忽略。微信 UI 可能
  合并短时间内的相同消息预览，因此高频或严格不丢消息的场景应使用 Plus 回调或其他渠道。
- 微信窗口最小化时，免费版轮询可能暂时看不到新的消息控件；恢复可见窗口后会等待会话
  预览与消息快照对齐，再补处理期间的新消息。长期运行仍应保持窗口可见且可交互。
- 消息归档默认保留 30 天，超出后清理。
- 这是 UI Automation 方案，并非微信官方 Bot API。账号风控与合规风险需要由使用者评估。
