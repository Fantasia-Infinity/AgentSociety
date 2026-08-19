# Hub DSH Web Bridge

本文档描述在 AgentSociety Hub 上为 DeepSeek Harness（dsh）Web 提供
**跨设备统一访问**的分阶段实现。第一阶段：节点可选择性向 Hub 注册
`dsh_web` 能力，Hub 保存并对外暴露脱敏的能力视图；P1（已实现）：
设备出站受控隧道 + Hub 同源 HTTP 代理。

## 目标与范围

- 每台设备继续运行自己的本地 `dsh web`（默认仅绑定 `127.0.0.1`，不做任何修改）。
  运行 `agent web-bridge` 时，如果目标地址尚未监听，Agent Host 会自动启动本地
  DSH Web，并在退出 bridge 时一并停止；若已经运行 `agent web`，则复用已有服务。
  可设置 `AGENT_DSH_WEB_BRIDGE_START=0` 禁用自动启动。
- 设备可选地通过环境变量 `AGENT_HUB_DSH_WEB=1` 声明自己提供 dsh Web 能力。
- Hub 在节点注册/心跳时保存 `dsh_web` 元数据，并仅在节点令牌（或管理员）认证下允许更新。
- 浏览器端可以通过受租户/主体作用域约束的 `GET /v1/hub/nodes` 读取每个节点的
  脱敏 `web` 视图（`enabled`、`protocol_version`、`profile` 等），用于设备选择器 UI。
- 设备通过 `agent web-bridge` 出站隧道接入 Hub，浏览器经 Hub 同源路径访问设备
  的 dsh Web HTTP 面（`/api` RPC 与静态资源）。

明确**不在**当前范围内（后续阶段设计）：

- 把设备本地 `.dsh/sessions` 文件或 live session 序列化后交给 Hub。
- 向浏览器下发节点令牌 / DeepSeek 模型凭据。
- Hub `/web` 直接反向代理远端 dsh UI（当前用受控隧道协议，见下）。
- DSH Web 前端通过相对 URL 原生支持挂载：API、插件、静态资源、manifest、favicon、
  HMR 与事件 WebSocket 都相对当前 `/v1/web/<node_id>/` 文档目录生成；Hub 不改写 HTML，
  也不向浏览器注入运行时补丁。

## 隧道协议（P1，已实现）

设备主动出站连接 Hub（NAT/防火墙友好），Hub 把浏览器请求路由回去：

1. 设备用节点令牌调用 `POST /v1/hub/nodes/web/tunnel`，获得一次性短时
   ticket（默认 120s）。
2. 设备连接 `GET /v1/web/tunnel/ws?ticket=<ticket>`（WebSocket 升级，
   纯标准库 RFC 6455 实现，`src/agent_hub/websocket.py`），ticket 消费后
   注册为该节点的活跃隧道（`src/agent_hub/tunnel.py`）。
3. 浏览器经 Hub 认证后访问 `GET/POST/HEAD /v1/web/<node_id>/<path>`，
   Hub 把请求打包为 JSON 消息经隧道转发；设备端 bridge
   （`agent-host/src/web-bridge.ts`，`agent web-bridge` 命令）fetch 本地
   `dsh web`（仅回环地址，默认 `http://127.0.0.1:3080`，可用
   `AGENT_DSH_WEB_TARGET` 覆盖）。`agent web-bridge` 默认会在目标端口没有服务时
   自动启动 `dsh --profile agent-society-web --port <port>`，并在 bridge 停止时回收
   这个子进程；如果目标已经可用，则不会启动第二个 DSH Web 实例。可用
   `AGENT_DSH_WEB_BRIDGE_START=0` 明确要求外部管理本地 DSH Web。bridge 将响应原样回传。
4. 浏览器 DSH 事件流：`GET /v1/web/<node_id>/api/events.{mux|host}`
   （WebSocket 升级）。Hub 先让设备打开本地
   `ws://127.0.0.1:<target>/api/events.{mux|host}`（ws-open/ws-open-ack），
   确认成功后升级浏览器连接，再把设备帧经隧道转发给浏览器
   （ws-frame；downlink-only，浏览器上行帧关闭 1008，与 dsh 语义一致）。
   设备本地事件流断开时浏览器端随之关闭。
5. 安全边界：路径白名单 `/`、`/api`、`/api/*`、`/assets/*`；事件流仅
   `/api/events.mux`、`/api/events.host`；请求头白名单（Accept/Content-Type/
   X-Requested-With）、响应头白名单；Hub 凭据/Cookie 绝不转发给设备。
   请求体上限 16MiB、响应 32MiB、代理超时 30s，设备离线返回 502。
   浏览器访问需 Hub 认证（Bearer 或 Hub web 会话 Cookie），且目标节点
   必须声明 `dsh_web` 能力并位于调用者的租户/主体作用域内。

消息契约（Hub↔设备，WebSocket text JSON）：

```jsonc
// Hub -> 设备
{"type":"http","id":"<request-id>","method":"POST","path":"/api/session.list",
 "headers":{...},"body_b64":"<base64|null>"}
{"type":"ws-open","id":"<stream-id>","path":"/api/events.mux"}
{"type":"ws-close","id":"<stream-id>","code":1000}
// 设备 -> Hub
{"type":"http-response","id":"<request-id>","status":200,"headers":{...},
 "body_b64":"<base64|null>"}
{"type":"ws-open-ack","id":"<stream-id>","ok":true,"error":null}
{"type":"ws-frame","id":"<stream-id>","opcode":1,"payload_b64":"<base64>"}
{"type":"ws-close","id":"<stream-id>","code":1000}
// 保活（预留）
{"type":"ping"} / {"type":"pong"}
```

浏览器侧端点语义：

- `GET /v1/web/<node_id>/api/...`（HTTP）：普通代理调用，业务错误保持
  dsh 契约（200 + ServerResponse）。
- `GET /v1/web/<node_id>/api/events.{mux|host}`（WS 升级）：事件 downlink，
  帧内容为 dsh `ServerRequest` JSON（`{"type":"server-request",...}`），
  与本地 dsh 的 `MUX_EVENTS_PATH`/`HOST_EVENTS_PATH` 帧完全一致。

## 契约

### 注册（`POST /v1/hub/nodes`）

`NodeRegistration` 新增可选顶层字段 `dsh_web`（与 `metadata` 平级）：

```json
{
  "node_id": "node-mac",
  "actor_id": "dsh-mac",
  "display_name": "Mac",
  "capabilities": ["filesystem", "remote-worker"],
  "dsh_web": {
    "enabled": true,
    "protocol_version": "1",
    "dsh_version": "0.1.0-rc.5",
    "profile": "agent-society-web",
    "capabilities": ["session.read"]
  }
}
```

- `enabled` 必须为布尔值；`protocol_version` / `dsh_version` / `profile` 为可选
  短字符串；`capabilities` 为去重字符串列表。
- 校验通过后以规范化形态写入 `metadata.dsh_web`，并自动把 `dsh-web` 加入节点
  `capabilities`。旧客户端（不带 `dsh_web` 字段）行为完全不变。

### 更新/心跳（`POST /v1/hub/nodes/web`）

```json
{ "node_id": "node-mac", "web": { "enabled": true, "protocol_version": "1" } }
```

- 节点令牌只能更新**自己**的节点；管理员可更新任意节点。
- 更新会同时刷新节点的 `status=online` / `last_seen_at`，可作为隧道阶段之前
  的能力心跳。本阶段节点端只在本机 `agent web` 启动时上报一次。

### 列表（`GET /v1/hub/nodes`）

每个节点响应新增 `web` 字段（脱敏视图，永远不包含 URL/凭据/工作区路径）：

```json
{ "node_id": "node-mac", "web": { "enabled": true, "protocol_version": "1", "profile": "agent-society-web" } }
```

未注册能力的节点返回 `"web": {"enabled": false}`。列表继续沿用现有
tenant/principal 作用域约束。

## 节点端（agent-host）

- 环境变量：`AGENT_HUB_DSH_WEB=1` 启用（默认关闭），
  `AGENT_HUB_DSH_WEB_PROFILE` 可选覆盖通告的 dsh profile（默认
  `agent-society-web`）。
- `agent web` 启动时（`cli.ts runDshWeb`）通过 `HubClient.updateNodeWeb` 上报；
  失败仅告警，不阻断 web 启动。
- `registerDshHost` / `registerHost` 在启用时把 `dsh_web` 元数据与 `dsh-web`
  能力写入节点注册。

## 安全说明

- 本阶段不暴露任何新监听端口；dsh web 依旧只绑定回环地址。
- 节点令牌绝不进入浏览器；浏览器读取的是脱敏视图。
- 未来 tunnel 阶段必须满足：节点出站连接 + Hub 同源路由 + 显式
  `/api`、`/api/events.mux`、`/api/events.host`、静态资源路径白名单 +
  每租户/主体/会话 ACL + 审计与限流。本阶段仅保留上述路径约束作为设计输入。

## P1 参考（DSH Web 权威契约）

代理/网关阶段应以 deepseek-harness 源码为权威依据，而不是 e2e 测试：

- RPC 方法名：`packages/host/apiproxy/src/api/rpc-map.ts`（12 个 session RPC）
- 请求/响应 schema：`packages/host/apiproxy/src/api/sessions.schema.ts`
- 实现契约：`packages/host/apiproxy/src/api/sessions.ts`
- HTTP dispatch/binding：`packages/host/apiproxy/src/fetch/handler.ts`
- Web API 客户端（单次调用 fetch，事件流 WS/SSE 组合）：
  `packages/host/apiproxy/src/fetch/client.ts`
- Session RPC 面（`POST /api/<method>`，非 REST；schema 行号见
  `sessions.schema.ts`）：`list`(64-72) / `create`(101-116, workspaceId 与 cwd
  二选一) / `rename`、`fork`(118-139) / `history`(141-146, 响应 237-242:
  events/hasMore/projections) / `models`、`selectModel`(244-268) /
  `prompt`(287-302, queue|steer, text/image) / `attachment`(317-327) /
  `updateQueue`(329-343) / `cancel`(345-353)
- 传输路径常量（`/api`、`/api/events.mux`、`/api/events.host`）：
  `packages/client/connection/src/api-path.ts`
- Hub 现有 SSE（`server.py` `/v1/hub/events`）是 worker 控制/共享事件通道，
  **不是** DSH session 事件流，不能作为代理传输。

### P1 传输硬约束（代理设计时不可违反）

- **浏览器事件通道是 WebSocket，不是 SSE**：`client/connection/src/client/web-api-client.ts`
  对 `/api/events.mux`、`/api/events.host`（`api-path.ts:7-14`）打开原生
  WS/WSS，接收 text JSON frames（处理 abort/close）；unary RPC 走 fetch。
  对事件端点直接普通 GET 会得到 426（`connection/src/index.ts:150-155`），
  因此**不支持 WebSocket Upgrade 的反代/代理不兼容**。
  （`?fixture` 会绕过网络，`client/index.ts:81-144`，不能作为远端集成依据。）
- WS 是 downlink 单向通道（`websocket-downlink.ts`）：仅 server→browser，
  客户端上行协议帧以 1008 关闭；上行只走 unary RPC。
- HTTP bridge（`http-bridge.ts`）可缓冲请求至 160MiB 并以 SSE 流式返回，
  这是 fetch 抽象层（`host/apiproxy/src/fetch/handler.ts`、`client.ts`）的客户端
  路径：`fetch/client.ts:352-404` 解析空行分隔的 `data:` JSON frame，
  畸形 frame 丢弃，标准 EventSource 语义不适用。Hub 若以 fetch 客户端身份
  接入设备才需要该 parser；对浏览器侧必须提供 WS Upgrade。
- 浏览器信任围栏（`api-request-trust.ts`）只校验 host/origin/browser，不是外部
  认证；特权方法仅限 loopback。Hub gateway 必须自行增加外部认证、ACL、审计、
  短期凭据管理与限流，并校验请求符合正式 schema。
- 认证缺口（P1 设计输入）：DSH 无认证 middleware，POST 仅要求 JSON（否则 415），
  export GET 仅信任围栏；**浏览器 WS 无法携带 Bearer 也无替代机制**
  （无 subprotocol/无 auth header），WS 无协商、无 keepalive、无 idle timeout、
  无断线 resume。因此：浏览器→Hub 用 Hub 自有 cookie/OIDC 会话认证；
  Hub→设备用独立短期凭据；代理必须自带 WS keepalive/idle 超时与断线策略，
  并为 fetch 路径实现严格 `data: ` parser（无 event/id/retry/Last-Event-ID、
  无恢复、未终止 final frame 丢弃）。

