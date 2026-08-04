# AgentSociety Hub 公网部署指南

本文描述把 `agent-hub` 部署到公网 VPS 的两种方式：**无域名测试模式**
（IP + 自签证书）和**正式域名模式**（Caddy 自动 HTTPS）。两种模式共用
PostgreSQL 与可选 S3 对象存储，并支持内嵌的 Web 管理界面。

## 架构

```text
公网设备 (./agent)
        │  HTTPS
        ▼
   Caddy（TLS / 限流 / 安全头）
        │  http://127.0.0.1:8090
        ▼
   agent-hub
        │
        ├── PostgreSQL（任务/租户/令牌）
        └── S3 / 本地文件（Artifact 内容）
```

Hub 容器只监听宿主机 loopback；公网流量必须经过 Caddy。默认单一共享 token
适合受控节点；开放多租户后每个租户和节点使用独立 token。

## 1. 服务器准备

- 一台公网 VPS（建议 Ubuntu 24.04，2 vCPU / 2GB 起步），安装 Docker 与
  Docker Compose v2。
- 防火墙只放行 `80/tcp` 与 `443/tcp`；**不要对外放行 8090**。
- 无域名模式：记录 VPS 公网 IP；正式域名模式：把 `hub.example.com` 的 A
  记录指向 VPS。

## 2. 无域名测试模式（自签证书）

在仓库 `deploy/hub` 目录下生成 CA 与服务器证书：

```bash
cd deploy/hub
AGENT_HUB_PUBLIC_IP=<VPS公网IP> bash scripts/generate-self-signed-cert.sh
```

生成 `certs/ca.pem`、`certs/hub.crt`、`certs/hub.key`。`certs/` 已被
Git 忽略，不要提交。把 `ca.pem` 分发给要接入的设备。

复制并修改环境变量：

```bash
cp ../../.env.hub.example .env.hub
```

Hub 的全部配置示例只维护在仓库根目录的 `.env.hub.example`；
`deploy/hub/.env.hub.example` 只是指向它的说明文件。

至少修改：

- `AGENT_HUB_TOKEN`：至少 24 字符，建议 `openssl rand -hex 24`。
- `AGENT_HUB_PUBLIC_URL=https://<VPS公网IP>`。
- `AGENT_HUB_POSTGRES_PASSWORD` 与 `AGENT_HUB_DATABASE_URL` 中的密码一致。
- `AGENT_HUB_WEB_SECRET`：至少 32 字符。
- 可选：`AGENT_HUB_OBJECT_STORE_URL=s3://bucket/prefix`，并设置 AWS 凭据。

启动：

```bash
docker compose up -d --build
```

验证：

```bash
curl --cacert certs/ca.pem https://<VPS公网IP>/health
curl --cacert certs/ca.pem -o /dev/null -w '%{http_code}\n' https://<VPS公网IP>/web
curl --cacert certs/ca.pem https://<VPS公网IP>/v1/hub/actors   # 应返回 401
```

## 3. 正式域名模式

在 `.env.hub` 中切换：

```dotenv
AGENT_HUB_TLS_MODE=letsencrypt
AGENT_HUB_DOMAIN=hub.example.com
AGENT_HUB_PUBLIC_URL=https://hub.example.com
```

然后重启 Caddy：

```bash
docker compose up -d --force-recreate caddy
```

Let's Encrypt 证书会自动签发和续期。自签证书目录此时不再使用。

## 4. Web 管理界面

访问 `https://<VPS公网IP>/web` 或 `https://hub.example.com/web`。使用
`AGENT_HUB_TOKEN`（原始值，不带 `Bearer `）登录。

管理员可以看到：

- 仪表盘统计和最近任务/Run。
- 任务列表、详情、事件流、创建/取消任务。
- Run、Artifact、Principal/Actor/Node 列表。
- 租户列表、租户详情、签发/吊销租户与节点 token。

Web 会话使用 `HttpOnly + SameSite=Strict` Cookie 和 CSRF 防护；登录 token
不会写入页面。租户 token 登录后只能看到本租户的数据。

## 5. 接入设备

正式域名模式下，直接运行：

```bash
./agent setup
```

Hub URL 填 `https://hub.example.com`，token 填 `AGENT_HUB_TOKEN` 或租户
token。无域名模式需要先让 Node.js 信任 CA：

```bash
export NODE_EXTRA_CA_CERTS=/path/to/certs/ca.pem
./agent setup
```

macOS/Windows/Linux 后台服务也要在启动环境里设置 `NODE_EXTRA_CA_CERTS`。

非 Pi agent（Codex/OpenCode 等）用 `./agent bridge --adapter <id>` 作为 worker，
并可用 MCP 作为派发端，详见 `docs/agent-adapters.md`。

## 5.1 MCP 接入（外部 Agent 派发任务）

Hub 内置 MCP Server：`POST /mcp`（streamable HTTP）与 `agent-hub-mcp`
（stdio）。Bearer token 与 `/v1/hub/*` 相同，租户 token 自动隔离租户数据。
Codex/OpenCode/Claude 等 MCP 客户端配置
`"url": "https://<VPS>/mcp"` + `Authorization` 头即可获得
`hub_create_task`、`hub_list_tasks`、`hub_get_task`、`hub_get_task_events`、
`hub_cancel_task` 等工具。默认开启，`AGENT_HUB_ENABLE_MCP=false` 可关闭。
详见 `docs/agent-adapters.md`。

## 6. 多租户与租户自带 worker

### 创建租户

```bash
curl -X POST https://hub.example.com/v1/hub/tenants \
  -H "Authorization: Bearer $AGENT_HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"team-a","display_name":"Team A"}'
```

### 为租户签发管理 token

```bash
curl -X POST https://hub.example.com/v1/hub/tenants/team-a/tokens \
  -H "Authorization: Bearer $AGENT_HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"role":"tenant_admin","principal_id":"human-owner","label":"team-a-admin"}'
```

返回体里的 `raw_token` 只显示一次，请立即保存。

### 租户注册自己的节点

租户管理员用租户 token 注册 Principal/Actor/Node，然后签发 node token：

```bash
curl -X POST https://hub.example.com/v1/hub/tenants/team-a/tokens \
  -H "Authorization: Bearer $TEAM_A_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"role":"node","actor_id":"pi-worker-1","node_id":"worker-1","label":"worker-1"}'
```

worker 设备配置：

```dotenv
AGENT_HUB_URL=https://hub.example.com
AGENT_HUB_TOKEN=<租户管理 token，用于 setup/register>
AGENT_HUB_NODE_TOKEN=<node token，用于 worker 领取任务>
```

node token 只能领取本租户任务、心跳自己的节点、更新自己持有的 Run。

### 可选 OIDC 登录

安装 `pip install '.[oidc]'`（Docker 镜像已包含），然后设置：

```dotenv
AGENT_HUB_OIDC_ISSUER=https://accounts.example.com
AGENT_HUB_OIDC_AUDIENCE=agent-society-hub
```

OIDC 用户的 `sub` 必须先用
`store.register_oidc_identity(...)`（或未来的管理 API）映射到已有
Principal 和租户，才能登录 Web 或调用 API。

## 7. 备份与恢复

PostgreSQL：

```bash
docker compose exec -T postgres pg_dump -U agenthub agenthub > hub-backup.sql
```

恢复：

```bash
cat hub-backup.sql | docker compose exec -T postgres psql -U agenthub agenthub
```

S3 bucket 开启版本控制，并按需做跨区复制。SQLite 本地部署的升级方式：
先在旧环境安装 `.[postgres]`，再运行
`agent-hub-migrate-postgres --source hub-state.sqlite3 --database-url ...`。

## 8. 安全边界

- 当前 bootstrap token 是管理员级；任何拿到它的人都能管理所有租户。
- 多租户隔离由 `tenant_id` 在存储层强制；跨租户任务委派不支持。
- Hub 只做协调，不为租户任务提供沙箱；租户 worker 由设备所有者控制。
- 逐节点 token 吊销/重签已支持；公网开放前仍需：审计保留、速率限制、Run 隔离。
