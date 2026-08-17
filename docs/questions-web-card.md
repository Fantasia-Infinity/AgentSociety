# Web 问题卡片（模式二）— spike 结论与实现规范

> 状态：**spike 已完成，host 侧已落地**；浏览器卡片组件需在带浏览器的机器上
> 验证后合入。本文件是验证与实现的操作手册。

## 1. Spike 结论（已确认，2026-08）

在 combo 固定的 deepseek-harness 提交上检查了浏览器客户端的 slot 系统：

- Web shell 只做一次 `ctx.slots.renderSlot('root', {})`
  （`packages/client/web/src/app.tsx`）。
- `@deepseek-ai/dsh-client-ui-layout` 向 `root` 注册 `AppFrame`，并声明四个
  子槽位（`packages/client/ui-layout/src/client/index.ts`）：

  | 槽位 | kind | scope | 用途 |
  |---|---|---|---|
  | `sidebar` | single | root | 左侧导航列（整列替换） |
  | `conversation` | single | session-maybe | 中间会话面（整列替换） |
  | `details` | single | session | 右侧详情列 |
  | **`shell.overlay`** | **list** | **root** | 叠加层（可多注册者共存） |

- **`shell.overlay` 是 `kind: 'list'` + `scope: 'root'`**：list 槽位允许多个
  注册者同时贡献内容，且不绑定当前 session。这是问题卡片的正确位置
  （模态/横幅语义，类似审批卡），**外部 client 插件注册即可，无需修改
  web-app / ui-layout 源码** —— 与用户要求的"不改 UI 本体、做成插件"
  一致。
- 注册写法（沿用 `session-log-export` 的 `ctx.slots.inject` 惯例）：

  ```ts
  ctx.slots.inject('shell.overlay', () =>
    ctx.slots.register({ name: 'agent-society-questions', children: {} }, QuestionCards),
  )
  ```

  组件 props 来自 `@deepseek-ai/dsh-client-ui-slots` 的
  `PropsRuntime & PropsRenderSlots<...> & PropsStore`（见
  `ui-layout/src/client/index.ts` 的注释：注册者 import 这些类型并交叉）。

## 2. 数据流（host ↔ client）

```
Hub ──SSE/poll──▶ host: agent-society-question-bridge（本仓库 dsh-plugin）
                   ├─ 无人/standalone → 自动回答（已实现，见 question-bridge.ts）
                   └─ 人在场/ask → 问题留给卡片（pending）
host ──RPC──▶ client: QuestionCards（shell.overlay 注册）
               ├─ 展示问题卡片（asker、问题、require、剩余时间）
               ├─ 批准 → host 注入会话由模型回答（复用 answer.ts）
               ├─ 直接回答 → 人打字即答案 → host answerQuestion
               └─ 拒绝 → host 标记 declined
client ──RPC──▶ host: setHumanPresent(present)（UI 活动事件驱动）
```

- host 侧 `ctx.reflect.provide('agentSocietyQuestionBridge', ...)` 已提供
  `setHumanPresent / humanPresent / policy / actorId / nodeId`。
- 浏览器 → host 的通道待真机验证（候选：bundle 的 package-private RPC /
  既有 remote gateway；见第 4 节）。

## 3. 人在场判定

- 浏览器 client 插件监听 UI 活动（点击/输入/焦点），把最后活动时间经 RPC
  上报 host；host 侧 `AGENT_SOCIETY_PRESENCE_WINDOW_MIN`（默认 5）内有人类
  活动 = 人在场。
- 未接上 client 时默认**不在场**（`policy='auto'` 会走自动回答）；这是
  保守选择：没有 UI 就没有人工审批的可能。
- `AGENT_SOCIETY_QUESTION_POLICY=auto|ask|standalone` 三值覆盖（已实现）。

## 4. 真机验证清单（在带浏览器的机器上执行）

1. `dsh web` 启动 `agent-society-web` profile（含本 bundle），确认
   `agent-society-question-bridge` 行已挂载（日志无 "stays idle"）。
2. 用第二个 worker 调用 `hub_ask` 问本机 actor：
   - 无人（浏览器未连接/超时窗口外）→ 自动回答回灌（当前已实现路径）；
   - 人在场（浏览器活跃）→ 问题保持 pending，等待卡片。
3. **浏览器卡片组件**：在 `dsh-plugin/src/client/` 按第 1、2 节实现
   `QuestionCards`（React.createElement，无 JSX 也可；或为 client 目录
   单独加 tsconfig + JSX），注册进 `shell.overlay`；验证：
   - 卡片出现/消失与 pending 问题同步；
   - 批准/直接回答/拒绝三条路径；
   - 人在场上报使自动回答停用；
   - 不打断当前会话的生成（卡片是 overlay，不注入 session）。
4. 若 bundle 的浏览器加载器不自动装载外部包的 `./client` 入口，记录
   需要的最小接线（仍在插件侧，不动 web-app 源码），回填本文件。

## 5. 已落地的 host 侧（本分支）

- `src/answer.ts`：共享的有界回答会话（ANSWER 标记提取、无本地工具、
  一次性 session），worker 与 bridge 共用。
- `src/question-bridge.ts`：`agent-society-question-bridge` 行 —— 轮询
  领取发给本 actor 的问题；`auto`（无人在场）/`standalone` 自动回答；
  `ask` 或人在场时留给卡片。
- `AGENT_SOCIETY_QUESTIONS=0` 可整机关闭。
