# AgentSociety Hub 密码账户认证设计（v1）

> 目标：废弃“共享随机 token 连接 Hub”的模式，改为用户注册 + 密码登录；每个用户的
> agent 通过自己设定的密码换取短期会话凭据连接 Hub，Web 提供注册/登录/账户管理设施。

## 1. 现状与废弃范围

旧模式（将被废弃的路径）：

- `AGENT_HUB_TOKEN` 共享 bootstrap token 不再用于 agent 连接和普通用户访问；
  生产部署已设置 `AGENT_HUB_DISABLE_BOOTSTRAP=1` 停用（token 仅存在于
  VPS `.env.hub`，且不接受登录/API 使用）；管理员日常操作走密码账户。
- 手工创建/分发长期 tenant/node API token 的方式不再作为推荐路径；
  API 保留兼容但标记 deprecated。

新模式：

- 用户账户：`username` + `password`（argon2id 哈希），绑定现有
  `principal_id`、`tenant_id`、角色（`tenant_admin` / `tenant_user`）。
- 会话：密码登录换取**短期会话凭据**（默认 24h），服务端只存哈希，可吊销。
- Agent 连接：`agent connect` 交互输入自己的用户名/密码 → Hub 校验后
  注册/绑定 principal/actor/node，并签发**节点短期凭据**（默认 7 天）；
  密码不落盘，节点凭据存入系统钥匙串，worker 启动时自动登录/续期。
- 无系统钥匙串的主机（如无头 Linux）：`agent connect` 会提示把
  `AGENT_HUB_NODE_TOKEN` 写入 `.env.agent`（文件权限 600）；该凭据由密码
  登录签发、可单独吊销，不等同于旧的共享 bootstrap token。
- Codex MCP 客户端通过 `agent-host/scripts/mcp-hub-wrapper.mjs` 从系统
  钥匙串读取**本机节点凭据**再桥接 mcp-remote，配置文件里不落任何 token；
  运行 `agent connect` 之后 MCP 工具即恢复可用。

## 2. 数据模型（新增表）

### hub_user_accounts

| 列 | 说明 |
|---|---|
| `username` | 全局唯一，登录名（正则 `[a-z0-9][a-z0-9._-]{2,63}`） |
| `password_hash` | argon2id 哈希串 |
| `principal_id` | 关联 `hub_principals`（每用户一个 principal） |
| `tenant_id` | 所属租户 |
| `role` | `tenant_admin` / `tenant_user` |
| `failed_attempts` / `locked_until` | 失败锁定（默认 5 次锁 15 分钟） |
| `created_at` / `updated_at` | 时间戳 |

### hub_auth_sessions

| 列 | 说明 |
|---|---|
| `session_token_hash` | sha256(原始随机会话令牌)，唯一 |
| `principal_id` / `tenant_id` / `role` | 会话身份 |
| `label` | 展示用（如 `web` / `agent-login` / 设备名） |
| `expires_at` / `revoked_at` / `last_seen_at` | 生命周期 |

节点凭据继续复用 `hub_auth_tokens`（role=`node`），但只由
`/v1/auth/agent-login` 用密码签发，不再手工分发。

## 3. 接口

### 公开（无需登录）

- `POST /v1/auth/register` `{username, password, display_name}` → 创建账户。
  - 第一个注册到某租户的用户自动成为该租户 `tenant_admin`，其余为 `tenant_user`。
  - 可用 `AGENT_HUB_ALLOW_REGISTRATION=0` 关闭公开注册。
- `POST /v1/auth/login` `{username, password}` → `{session_token, expires_at, user}`。
- `POST /v1/auth/agent-login`
  `{username, password, node_id, actor_id, display_name, capabilities, metadata}` →
  校验密码，幂等注册 principal/actor/node，签发 node 短期凭据
  → `{node_token, node, actor, principal, tenant_id}`。

### 需要会话/节点凭据

- `GET /v1/auth/me` → 当前用户、账户、自己的会话与节点凭据列表。
- `POST /v1/auth/logout` → 吊销当前会话。
- `POST /v1/auth/change-password` `{old_password, new_password}` → 改密
  （改密后吊销除当前会话外的所有会话与节点凭据）。
- `POST /v1/auth/sessions/revoke` `{session_token_id}` → 吊销指定会话。
- `POST /v1/auth/tokens/revoke` `{token_id}` → 吊销自己的节点凭据。

## 4. Web 界面

- `/web/register`：注册页（用户名/密码/显示名）。
- `/web/login`：用户名+密码登录；保留“使用管理员令牌”折叠入口。
- `/web/account`：我的账户（用户名/角色/租户）、改密、会话列表与吊销、
  已连接节点凭据列表与吊销。
- `/web/logout`：登出。

## 5. agent-host 连接流程

```
agent connect
  ├─ 输入 username + password（隐藏输入）
  ├─ POST /v1/auth/agent-login
  ├─ 将 node_token 写入系统钥匙串（AGENT_HUB_NODE_TOKEN_CREDENTIAL_*）
  ├─ 将 username 引用写入 .env.agent（不写密码明文）
  └─ 打印节点/租户信息

worker 启动
  ├─ 读钥匙串 node_token（或 AGENT_HUB_NODE_TOKEN 兼容项）
  ├─ 401/过期 → 用钥匙串中的密码重新 agent-login（仅内存持有新凭据）
  └─ 全部失败 → 提示运行 `agent connect`
```

## 6. 安全基线

- 密码 argon2id（`argon2-cffi`，time_cost=3, memory_cost=65536, parallelism=4）。
- 密码强度：至少 10 位，含字母与数字。
- 登录/注册限速：按用户名/IP 失败计数 + 锁定；恒时比较。
- 会话/节点凭据服务端只存 sha256；Web 会话沿用 HMAC cookie
  （HttpOnly + Secure + SameSite=Strict + CSRF）。
- 全部走 HTTPS；MCP/A2A/REST 统一走 Bearer（会话或节点凭据）。

## 7. 迁移

- 现有 principal/actor/node/task 数据不动，归入各自 tenant。
- 旧共享 token 立即失效（已完成轮换）；API token 兼容保留到显式吊销。
- Web 管理员先用 bootstrap token 登录 → 注册自己的账户 → 后续用密码登录。
