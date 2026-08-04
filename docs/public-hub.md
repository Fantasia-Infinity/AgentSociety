# AgentSociety Hub 公网部署指南

本文描述把 `agent-hub` 部署到公网 VPS 的推荐方式：**Cloudflare Tunnel**
（无域名测试用 Quick Tunnel，正式域名用 Named Tunnel，HTTPS 由 Cloudflare
边缘自动提供），以及可选的**直连 Caddy** 模式（自签证书或 Let's Encrypt）。
两种方式共用 PostgreSQL 与可选 S3 对象存储，并支持内嵌的 Web 管理界面。

## 架构

```text
公网设备 (./agent)
        │  HTTPS
        ▼
Cloudflare 边缘（TLS / DDoS 防护）
        │  cloudflared 出站隧道（无需开放入站端口）
        ▼
   agent-hub (127.0.0.1:8090)
        │
        ├── PostgreSQL（任务/租户/令牌）
        └── S3 / 本地文件（Artifact 内容）
```

`cloudflared` 容器只做出站连接；Hub 容器仍只监听宿主机 loopback，公网流量
经 Cloudflare 边缘进入。默认单一共享 token 适合受控节点；开放多租户后每个
租户和节点使用独立 token。

可选直连模式（不使用 Cloudflare）：`docker compose --env-file .env.hub --profile caddy up -d`
会额外启动 Caddy 并发布 80/443，TLS 由自签证书或 Let's Encrypt 处理。

## 1. 服务器准备

- 一台公网 VPS（建议 Ubuntu 24.04，2 vCPU / 2GB 起步），安装 Docker 与
  Docker Compose v2。
- Cloudflare 模式：**不需要放行任何入站端口**，服务器只需能出站访问
  Cloudflare（443/7844）；**不要对外放行 8090**。
- 直连 Caddy 模式：防火墙放行 `80/tcp` 与 `443/tcp`。
- 无域名测试：不需要 Cloudflare 账号；正式域名：创建 Named Tunnel 后在
  Cloudflare 控制台配置 `hub.example.com` 的 Public Hostname。

## 2. 无域名测试模式（Cloudflare Quick Tunnel，推荐）

无需账号和域名，启动后得到随机的 `https://<随机ID>.trycloudflare.com`
地址。该地址每次重启都会变化，只适合短期测试。

在 `deploy/hub` 目录下复制并修改环境变量：

```bash
cd deploy/hub
cp ../../.env.hub.example .env.hub
```

Hub 的全部配置示例只维护在仓库根目录的 `.env.hub.example`；
`deploy/hub/.env.hub.example` 只是指向它的说明文件。

至少修改：

- `AGENT_HUB_TOKEN`：至少 24 字符，建议 `openssl rand -hex 24`。
- `AGENT_HUB_POSTGRES_PASSWORD` 与 `AGENT_HUB_DATABASE_URL` 中的密码一致。
- `AGENT_HUB_WEB_SECRET`：至少 32 字符。
- 可选：`AGENT_HUB_OBJECT_STORE_URL=s3://bucket/prefix`，并设置 AWS 凭据。

启动：

```bash
docker compose --env-file .env.hub up -d --build
docker compose --env-file .env.hub logs -f cloudflared
```

从日志复制 `https://<随机ID>.trycloudflare.com`，写入 `.env.hub` 的
`AGENT_HUB_PUBLIC_URL` 并重建 Hub（让 A2A Agent Card 返回正确地址）：

```bash
AGENT_HUB_PUBLIC_URL=https://<随机ID>.trycloudflare.com
docker compose --env-file .env.hub up -d --force-recreate agent-hub
```

验证（证书由 Cloudflare 签发，无需 `--cacert`）：

```bash
curl https://<随机ID>.trycloudflare.com/health
curl -o /dev/null -w '%{http_code}\n' https://<随机ID>.trycloudflare.com/web
curl https://<随机ID>.trycloudflare.com/v1/hub/actors   # 应返回 401
```

设备端直接使用该 HTTPS 地址即可，无需信任任何自签 CA。

## 3. 正式域名模式（Cloudflare Named Tunnel）

需要 Cloudflare 账号 + 域名（免费套餐即可，最多 50 条隧道）。在
Cloudflare Zero Trust → Networks → Tunnels 创建隧道，选择 token 方式；
在 Public Hostnames 里把 `hub.example.com` 的 ingress 指向
`http://agent-hub:8090`（cloudflared 容器与 Hub 在同一 Docker 网络，
可直接使用该地址）。

在 `.env.hub` 中配置 token：

```dotenv
AGENT_HUB_CLOUDFLARE_TUNNEL_TOKEN=<创建隧道时获得的 token>
AGENT_HUB_PUBLIC_URL=https://hub.example.com
```

停掉 quick tunnel，启动 named tunnel 并重建 Hub：

```bash
docker compose --env-file .env.hub stop cloudflared
docker compose --env-file .env.hub --profile named-tunnel up -d cloudflared-named agent-hub
```

Cloudflare 在边缘自动签发并续期可信证书，设备端无需任何额外证书配置。

### 可选：直连 Caddy 模式（不用 Cloudflare）

保留的原方案，TLS 由 Caddy 直接处理（自签证书需要先生成
`AGENT_HUB_PUBLIC_IP=<VPS公网IP> bash scripts/generate-self-signed-cert.sh`
并把 `certs/ca.pem` 分发给设备）：

```dotenv
AGENT_HUB_TLS_MODE=letsencrypt   # 或 self-signed（需 AGENT_HUB_PUBLIC_IP）
AGENT_HUB_DOMAIN=hub.example.com
AGENT_HUB_PUBLIC_URL=https://hub.example.com
```

```bash
docker compose --env-file .env.hub --profile caddy up -d --build
```

Let's Encrypt 证书会自动签发和续期；自签证书目录此时不再使用。

## 4. Web 管理界面

访问 Quick Tunnel 地址的 `/web`（如 `https://<随机ID>.trycloudflare.com/web`）
或 `https://hub.example.com/web`。使用
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

Hub URL 填 `https://hub.example.com` 或 Quick Tunnel 的
`https://<随机ID>.trycloudflare.com`，token 填 `AGENT_HUB_TOKEN` 或租户
token。Cloudflare 两种模式都不需要信任 CA；仅直连 Caddy 自签模式需要：

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
docker compose --env-file .env.hub exec -T postgres pg_dump -U agenthub agenthub > hub-backup.sql
```

恢复：

```bash
cat hub-backup.sql | docker compose --env-file .env.hub exec -T postgres psql -U agenthub agenthub
```

S3 bucket 开启版本控制，并按需做跨区复制。SQLite 本地部署的升级方式：
先在旧环境安装 `.[postgres]`，再运行
`agent-hub-migrate-postgres --source hub-state.sqlite3 --database-url ...`。

## 8. 安全边界

- 当前 bootstrap token 是管理员级；任何拿到它的人都能管理所有租户。
- 多租户隔离由 `tenant_id` 在存储层强制；跨租户任务委派不支持。
- Hub 只做协调，不为租户任务提供沙箱；租户 worker 由设备所有者控制。
- 逐节点 token 吊销/重签已支持；公网开放前仍需：审计保留、速率限制、Run 隔离。
