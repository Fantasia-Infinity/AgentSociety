# Mac 本地 RWKV

Bot Core 把本地 RWKV 作为独立的 OpenAI-compatible 服务使用。模型进程崩溃或升级时
不会带崩 Core。`local_rwkv` 的生成和健康检查地址都必须使用 loopback；若将来需要把
推理服务迁到局域网服务器，应新增一个明确的受信任网络后端，而不是放宽本地模式边界。

## 支持的路由模式

| `LLM_BACKEND` | 行为 |
| --- | --- |
| `remote` | 只调用现有远程 API，默认值 |
| `local_rwkv` | 只调用本地 RWKV；不可用时让持久化 Inbox 重试 |
| `auto` | 先调用本地 RWKV，失败后把同一会话发送到远程 API |

`auto` 会改变隐私边界，只应在明确接受远程回退时使用。Core 不会因为本地模型健康
检查失败而拒收 Gateway 消息；消息会先进入 SQLite，再按原有退避策略重试。

## llama.cpp 服务

RWKV 官方文档说明 llama.cpp 支持 RWKV-6/7。Apple Silicon 可以使用 macOS arm64
预编译版本，或者通过 Homebrew 安装当前 llama.cpp。远程模型继续使用
`/v1/chat/completions`；本地 RWKV Provider 会把统一的消息历史转换成
`System/User/Assistant` 文本格式，再调用 `/v1/completions`。

真实测试确认当前 llama.cpp 10210 的 `rwkv-world` Chat Completions 模板没有正确传入
这个 GGUF 的消息正文，而官方 completion 格式可以正常中文问答。因此不能只把远程
`LLM_BASE_URL` 改成本机地址，必须保留 Core 中的 RWKV prompt 适配层。

- RWKV 指南：<https://github.com/RWKV/RWKV-wiki/blob/main/docs/inference/llamacpp.md>
- llama-server：<https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>

模型权重不要提交到 Git。建议放到：

```text
~/Library/Application Support/WechatBot/models/
```

下载权重后记录来源、模型版本、量化类型和 SHA-256。首次测试建议从 1–3B 量化 GGUF
开始，再根据首字延迟、生成速度和峰值内存决定是否扩大模型。

这台 16 GB M1 Pro 的第一候选是
[`zhiyuan8/RWKV-v7-1.5B-G1-GGUF`](https://huggingface.co/zhiyuan8/RWKV-v7-1.5B-G1-GGUF)
的 Q8_0 版本；仓库标注大小约 1.69 GB。它适合先验证模板、Metal 和完整微信链路，
通过后再与 2.9B Q8_0 做质量和延迟对比。模型仓库由 RWKV 官方 llama.cpp 指南链接，
权重基于 Apache-2.0 的 `BlinkDL/rwkv7-g1`。

当前已验证基线（2026-08-02）：

| 项目 | 值 |
| --- | --- |
| llama.cpp | Homebrew `10210`，Darwin arm64 |
| Hugging Face revision | `93e3a72ac6e99c7eb9c742da92b8bad3bc24e946` |
| 文件 | `rwkv7-1.5B-g1-Q8_0.gguf` |
| 文件大小 | `1692237824` bytes |
| SHA-256 | `1dd40d7f1fee50bf5125194540fbcfd3831564ff5000c18b873a42daa8ec311b` |
| M1 Pro 实测 | 约 55–60 tokens/s，缓存权重约 0.43 秒加载 |

## 配置和手动启动

在项目 `.env` 中保留远程配置，并增加：

```dotenv
LLM_BACKEND=local_rwkv
LOCAL_LLM_BASE_URL=http://127.0.0.1:18080/v1
LOCAL_LLM_MODEL=rwkv-local
LOCAL_LLM_EXECUTABLE=/opt/homebrew/bin/llama-server
LOCAL_LLM_MODEL_PATH=/absolute/path/to/rwkv-model.gguf
LOCAL_LLM_BIND_HOST=127.0.0.1
LOCAL_LLM_PORT=18080
LOCAL_LLM_CONTEXT_SIZE=4096
LOCAL_LLM_MAX_CONCURRENCY=1
LOCAL_LLM_THREADS=8
LOCAL_LLM_TEMPERATURE=1.0
LOCAL_LLM_TOP_P=0.5
LOCAL_LLM_REPEAT_PENALTY=1.2
LOCAL_LLM_GPU_LAYERS=99
LOCAL_LLM_CHAT_TEMPLATE=rwkv-world
```

先启动模型服务：

```bash
PYTHONPATH=src python3 -m wechat_bot.local_model
```

另一个终端验证：

```bash
curl -sS http://127.0.0.1:18080/health
curl -sS http://127.0.0.1:18080/v1/models
```

然后重启 Core。Core 的 `/health` 会包含不含聊天内容和密钥的模型状态，例如：

```json
{"status":"ok","queue_depth":0,"model":{"backend":"local_rwkv","status":"ready","max_concurrency":1}}
```

## LaunchAgent

模板位于
`deploy/macos/com.fantasia.wechat-bot-local-llm.plist.example`。复制到
`~/Library/LaunchAgents/com.fantasia.wechat-bot-local-llm.plist` 前替换：

- `__PYTHON__`：实际 Python 或虚拟环境解释器的绝对路径。
- `__PROJECT_DIR__`：稳定的项目目录，不要使用可能被删除的临时 worktree。
- `__LOG_DIR__`：预先创建的用户日志目录。

模板使用 `RunAtLoad=true` 和 `KeepAlive=true`。模型和路径确定后再加载 LaunchAgent，
否则 launchd 会按照 KeepAlive 持续重试无效配置。Core 与模型服务没有强启动顺序：
模型尚在加载时，Core 仍然接收消息并持久化；模型 `/health` 就绪后重试会自动继续。

## 第一阶段限制

- Core 仍从 SQLite 读取最近对话并随每次请求发送，不保存 RWKV 隐藏状态。
- 本地推理服务只监听 loopback，不允许配置成 `0.0.0.0`。
- Core 也拒绝非 loopback 的 `LOCAL_LLM_BASE_URL` 和 `LOCAL_LLM_HEALTH_URL`。
- 启动器禁用 Web UI，并把 CORS 限制为 localhost，避免普通网页跨域滥用本机模型。
- 首版只处理文本聊天；启动器默认显式选择 llama.cpp 内置的 `rwkv-world` 模板，避免
  没有模板元数据的 GGUF 被错误地按 ChatML 处理。
