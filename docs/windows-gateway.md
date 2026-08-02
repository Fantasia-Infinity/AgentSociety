# Windows Gateway 指南

## 推荐形态

微信客户端与 Gateway 放在同一台原生 Windows 设备上；Bot Core 继续运行在 Mac、局域网
主机或服务器。以后本地 LLM 也放在服务器侧，Windows 端无需升级显卡或修改微信适配器。

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

免费包没有 `AddListenChat` 时，Gateway 会自动使用前台轮询。启动时先把当前可见消息记为
基线，只上报之后出现的新消息；轮询会逐个切换 `WECHAT_LISTEN_CHATS` 中的会话，因此建议
首轮只配置一个专用测试好友，并保持微信窗口可交互。

## 配置

```powershell
Copy-Item .env.gateway.example .env.gateway
notepad .env.gateway
```

关键项示例：

```dotenv
GATEWAY_ACCOUNT_ID=my-wechat-pc
BOT_CORE_URL=https://bot.example.com
BOT_API_TOKEN=与Core完全相同的长随机值
WECHAT_DRIVER=wxautox4
WECHAT_LISTEN_CHATS=文件传输助手,家人群
WECHAT_BOT_MENTION=@你的微信昵称
WECHAT_POLL_INTERVAL_SECONDS=1
```

- `WECHAT_LISTEN_CHATS` 是明确允许监听的好友备注名/群名，使用英文逗号分隔。
- wxauto 暴露的是界面显示名称而非稳定微信 ID，因此改备注、群名重名都会影响路由。
- Core 的 `BOT_ALLOWED_USERS` / `BOT_ALLOWED_GROUPS` 要填写相同的界面名称。
- 群聊默认只有文本中包含 `WECHAT_BOT_MENTION` 才会进入模型。
- `WECHAT_POLL_INTERVAL_SECONDS` 只影响没有回调接口的免费版，允许范围为 1–60 秒。
- `GATEWAY_STATE_DB` 保存已发送动作 ID，不能放在会被定期清理的临时目录。
- 同一个 SQLite 文件还保存 Gateway Inbox 与每个聊天的历史同步游标。消息会先落库再上传
  Core，因此 Core 短暂离线时不会因为 HTTP 失败直接丢失已采集消息。

如果 Core 暂时运行在 Mac，把 `.env` 中 `BOT_API_HOST` 改成 `0.0.0.0`，并将
`BOT_CORE_URL` 指向 Mac 的局域网地址。明文 HTTP 只适合可信局域网；跨公网应使用 HTTPS
反向代理或私有组网，不要直接暴露 8080 端口。

## 启动

先登录 Windows 微信并保持窗口可用，然后运行：

```powershell
.\.venv\Scripts\python.exe -m wechat_gateway
```

### 长期运行

微信必须保持已登录且 Windows 用户会话保持可交互。可将 Gateway 注册为当前用户登录时
自动启动的计划任务；Gateway 断开 Core、微信退出或进程异常结束时会自动重试/拉起：

```powershell
.\scripts\install-gateway-task.ps1
```

任务名为 `WechatBotGateway`。运行日志写入 `gateway-logs/`，不包含 API token。查看、停止或
手动启动任务：

```powershell
Get-ScheduledTask -TaskName WechatBotGateway
Stop-ScheduledTask -TaskName WechatBotGateway
Start-ScheduledTask -TaskName WechatBotGateway
```

Mac 上的 Bot Core 也必须以持续运行的后台服务方式启动；如果 Mac 用户会话退出或 Core
进程停止，Windows Gateway 只会缓存/重试，无法自行恢复 Core 进程。

wxauto 依赖可交互桌面。Windows 锁屏、RDP 断开后切换到不可交互会话、微信升级或窗口
结构变化都可能中断监听。官方的[云服务器部署说明](https://docs.wxauto.org/deploy.html)
也明确要求窗口保持活跃。

## 首次测试清单

1. 先只监听“文件传输助手”或专用测试好友/群。
2. Core 使用严格 allowlist，确认未列入的会话不会调用模型。
3. 给测试会话发一条普通文本，检查 Gateway 的 `event_uploaded` 日志。
4. 检查 Core 生成动作，以及 Gateway 的 `action_sent`、`action_acked` 日志。
5. 暂停 Core 网络后恢复，确认没有重复回复。
6. 再逐个增加监听会话，避免高频、群发或营销式自动操作。

## 已知限制

- 当前只回复文本；图片、语音、文件会被标准化，但 Core 会拒绝为不支持的内容类型。
- 免费版轮询启动时有约 3 秒预热，预热期间出现的消息会被当作基线忽略。微信 UI 可能
  合并短时间内的相同消息预览，因此高频或严格不丢消息的场景应使用 Plus 回调或其他渠道。
- 微信窗口最小化时，免费版轮询可能暂时看不到新的消息控件；恢复可见窗口后会等待会话
  预览与消息快照对齐，再补处理期间的新消息。长期运行仍应保持窗口可见且可交互。
- 微信客户端退出后，免费版会按指数退避尝试重建 UI 连接。重新登录后会使用持久聊天游标
  回补微信 UI 当前仍能加载的历史消息。
- Gateway 会从 SQLite Inbox 和聊天游标恢复上传；微信 UI 能够加载出的历史消息会在启动/重连
  时回补。若消息已经被微信客户端清理、尚未同步到 UI 或超出 UI 可加载范围，wxauto 无法
  保证获取。
- Core 的收件箱、会话、去重和 Outbox 已持久化；正式服务器化时可将 SQLite 替换为持久队列
  和 PostgreSQL。
- 这是 UI Automation 方案，并非微信官方 Bot API。账号风控与合规风险需要由使用者评估。
