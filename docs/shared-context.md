# 共享共识上下文、会话目录与按需问答

本页汇总 `feat/shared-context` 分支引入的三组相互咬合的能力：跨设备共享记忆
（共识上下文）、session/Agent 信息目录（递进式查询）与任务中按需问答/委派。
全部实现为**增量端点与插件行**，旧 worker 无需升级即可连接新 Hub。

## 1. 交互协议（P1）

- **长轮询 claim**：worker 的 `claimTask` 传 `wait_seconds=min(pollSeconds,30)`，
  Hub 端条件等待，任务派发延迟从轮询间隔降到 ~0。
- **SSE 推送通道** `GET /v1/hub/events?node_id=`：`control/new`、
  `task/cancelled`、`shared/event`、`directory/updated`、`question/new`、
  `question/answered`；断线带 `after_seq` 续传，不可用时全部降级回轮询。
- **部分结果**：`updateTask` 接受 `partial_result`，追加为
  `task.partial_result` 事件；worker 每 15s 上报（工具数、最近工具）。
- 能力协商：worker actor 注册 `push` / `ask` 能力。

## 2. 共识上下文（P2）

Hub 上每个 principal 一份**追加式共享记忆**（表 `hub_shared_events`，
scope=`consensus` / `directory` / `qa`）：

- 幂等 append（`event_id` 内容哈希）；增量拉取（`after_seq`）；TTL（默认
  digest 30d、fact/decision 90d、answer 180d）；后台定期清理。
- REST：`POST /v1/hub/contexts/append`、`GET /v1/hub/contexts`、
  `GET /v1/hub/contexts/snapshot`。
- MCP：`hub_context_append`、`hub_context_read`。
- worker 在任务 run 结束后（`AGENT_SOCIETY_CONTEXT=1`）写入确定性 digest
  （idempotent event_id、结果截断 1000 字符）。

## 3. 信息目录（P3）

每 session 一行、按深度递进：0 身份 → 1 调用记录 → 2 共识摘要 → 3 transcript
引用。

- REST：`PUT /v1/hub/directory/{session_id}`、`GET /v1/hub/directory`（增量、
  文本/状态/actor 过滤）、`GET /v1/hub/directory/{session_id}?depth=0..3`。
- MCP：`hub_directory_list` / `hub_directory_get` / `hub_directory_search`。
- 设备侧：`agent-society-directory` 行把本地 session（dsh 持久化 + 投影缓存
  标题）推送上去，并增量拉取到本地镜像
  `~/.dsh/agent-society-directory.json`（0600）；worker 在 run 结束时合并
  调用记录（去重、最多 10 条）再推送。
- 陈旧淘汰：idle >30d 的目录行过期隐藏。

## 4. 缓存友好的有界上下文注入（P4）

agent-society-directory-index 默认使用 AGENT_SOCIETY_CONTEXT_MODE=balanced：

- 第一次进入某个 session 时，通过公开 agent/pre-step waterfall 追加最多
  1600 字符的紧凑初始快照；
- 后续只追加最多 800 字符的结构化 delta；没有语义变化就不追加消息；
- 共识按 seq 倒序且每个 session 默认只留最新一条，目录只按
  session_id 排序；last_active_at、invocations 和对象插入顺序不会触发
  更新；
- 目录/共识被删除、过期或 delta 无法容纳时追加一次新的紧凑快照；
- 每条更新包含 directory/consensus revision。完整 objective/result 与目录
  详情仍通过 Hub MCP 按需读取；自动注入只使用摘要字段；
- 更新作为普通 AgentSociety user message 进入 session，不设置
  startsRequestSeries，不调用 dsh surface replace。已有旧 session 中的
  snapshot 不做破坏性重写。

兼容和回滚：

- balanced：默认的初始快照 + delta；
- legacy：恢复原有 system-prompt/assemble 的完整有界 runtime snapshot；
- off：只保留静态 workflow 说明，不自动注入共享数据；
- AGENT_SOCIETY_DIRECTORY=0 仍是硬关闭开关；
- AGENT_SOCIETY_CONTEXT=0 仍只关闭 digest 写入，不改变历史语义。

## 5. 按需问答与委派（P5 / P6a）

**阻塞式提问 `hub_ask`**（MCP 工具，worker/TUI/Web 通用）：创建 question 后
阻塞（默认 60s，上限 300s），返回 answer / timeout / expired / unsupported。

- 表 `hub_questions`：pending → claimed → answered | expired | unsupported；
  lease/claim/answer；答案写入共享记忆（qa scope）并在 asker 任务存在时写
  `question.answered` 任务事件。
- **回答方**：
  - 无人 worker（`agent-society-worker`）：空闲时领取并回答（一次性无工具
    会话、`ANSWER:` 标记提取），绝不打断运行中的任务；
  - 交互进程（`agent-society-question-bridge`）：`auto`（无人在场自动答 /
    人在场留给卡片）、`ask`（留给卡片）、`standalone`（总是自动答）；
  - 浏览器问题卡片（模式二）：`shell.overlay` 槽位（list/root），client
    插件注册即可，无需改 web-app —— 组件实现见
    [questions-web-card.md](questions-web-card.md)（待真机验证）。
- **委派**：worker/TUI/Web 内的 agent 直接使用既有 `hub_create_task`
  （`context_id` 关联当前任务即形成调用图）；无新权限（节点 token 只能以
  自己 actor 委派、同 principal）。

## 环境变量

| 变量 | 默认 | 含义 |
|---|---|---|
| `AGENT_SOCIETY_CONTEXT` | 未设 | `1` 时 worker 写入共识 digest |
| `AGENT_SOCIETY_DIRECTORY` | 未设(`0` 关闭) | 目录同步 + 注入守卫开关 |
| `AGENT_SOCIETY_DIRECTORY` | 启用(`0` 关闭) | 目录同步 + 注入守卫开关 |
| `AGENT_SOCIETY_CONTEXT_MODE` | `balanced` | `balanced` / `legacy` / `off` |
| `AGENT_SOCIETY_DIRECTORY_PULL_SECONDS` | 10 | 目录镜像拉取间隔 |
| `AGENT_SOCIETY_QUESTIONS` | 未设(`0` 关闭) | 问题回答（worker + bridge） |
| `AGENT_SOCIETY_QUESTION_POLICY` | auto | `auto` / `ask` / `standalone` |
| `AGENT_SOCIETY_QUESTION_POLL_SECONDS` | 10 | 交互进程问题轮询间隔 |
| `AGENT_SOCIETY_PRESENCE_WINDOW_MIN` | 5 | 人在场判定窗口（client 上报） |

AgentSociety 在 $DSH_HOME/agent-society-context-metrics.json 保存不含内容的
计数：初始/增量消息字节数、每 session 更新次数、digest 模型调用与被 lease
去重次数、Hub tool schema hash。文件只用于诊断，写入失败不影响运行；agent
doctor --json 可展示这些统计。

## 已知边界

- Hub 仍为标准库单进程（几十个 worker 节点规模）；SSE 断线全功能降级轮询。
- 会话内回答（问题注入目标 session 由其自身上下文回答）为后续增强；当前
  回答使用独立有界会话（前缀不变原则见 `docs/questions-web-card.md` 与
  `src/answer.ts`）。
- TUI 端问题卡片需改 dsh-TUI 源码，推迟（见 `questions-web-card.md`）。
- 跨 principal 协同不在本期。
