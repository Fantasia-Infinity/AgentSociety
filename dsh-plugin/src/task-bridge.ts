/**
 * `agent-society-task-bridge`: let the interactive UI session of this dsh
 * process (web) accept and execute Hub tasks inside its own conversation.
 *
 * The web process polls for tasks addressed to this actor. A task is only
 * claimed while the UI session's agent is idle (no turn in flight), and the
 * whole execution lands in the session history via `followup(TASK_PROMPT)` —
 * the human sees the run when they return, and the result is written back to
 * the Hub task plus a structured task digest into shared memory. A human
 * talking to the session keeps the agent busy, so the bridge simply waits for
 * the next idle round; tasks addressed to this actor are never executed in
 * worker sessions (those belong to the worker plugin).
 *
 * Enabled for the web surface via AGENT_SOCIETY_UI_TASKS=1.
 */

import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type {} from '@deepseek-ai/cordis-plugin-timer'
import Schema from '@deepseek-ai/schemastery'
import { hostname, userInfo } from 'node:os'
import { resolve } from 'node:path'

import { HubClient, type HubClaim } from './hub-client.js'
import { buildSessionDigest } from './digest.js'
import { lastAssistantText, TASK_PROMPT } from './worker-plugin.js'

export const name = 'agent-society-task-bridge'
export const inject = ['timer']

export interface Config {
  hubUrl?: string
  hubTokenEnv?: string
  principalId?: string
  actorId?: string
  nodeId?: string
  workspaceRoot?: string
  pollSeconds?: number
}

export const Config: Schema<Config> = Schema.object({
  hubUrl: Schema.string().required(false),
  hubTokenEnv: Schema.string().required(false),
  principalId: Schema.string().required(false),
  actorId: Schema.string().required(false),
  nodeId: Schema.string().required(false),
  workspaceRoot: Schema.string().required(false),
  pollSeconds: Schema.number().min(2).max(300).default(10),
})

export function apply(ctx: Context, config: Config): void {
  const hubUrl = config.hubUrl ?? process.env.AGENT_SOCIETY_HUB_URL?.trim()
  const tokenEnv = config.hubTokenEnv ?? 'AGENT_SOCIETY_HUB_TOKEN'
  const hubToken = process.env[tokenEnv]?.trim()
  if (!hubUrl || !hubToken) {
    ctx.logger.warn(
      'agent-society-task-bridge: Hub credentials are required; task bridge stays idle',
    )
    return
  }
  const owner = stableSlug(userInfo().username)
  const host = stableSlug(hostname())
  const principalId = config.principalId ?? `human-${owner}`
  const actorId = config.actorId ?? `agent-society-${host}`
  const nodeId = config.nodeId ?? host
  const workspaceRoot = resolve(config.workspaceRoot ?? process.cwd())
  const pollSeconds = config.pollSeconds ?? 10
  const LEASE_SECONDS = 900 // Hub max; a long UI task must not expire mid-run

  ctx.logger.warn(
    `agent-society-task-bridge active (hub=${hubUrl}, poll=${pollSeconds}s, actor=${actorId})`,
  )

  const hub = new HubClient(hubUrl, hubToken)
  const state = { busy: false }
  /** Claimed but not yet executed task (kept across polls until executed). */
  let activeClaim: HubClaim | null = null

  const timer = ctx.setInterval(() => {
    void poll()
  }, pollSeconds * 1_000)
  ctx.effect(() => () => timer())
  void poll()

  /** The first live UI-session agent that is idle, if any. */
  function idleSessionAgent(): Agent | undefined {
    const agents = ctx.get('agents') as
      | { list(): Agent[] }
      | undefined
    if (!agents || typeof agents.list !== 'function') return undefined
    for (const agent of agents.list()) {
      if (agent === undefined) continue
      if (typeof agent.status !== 'string') continue
      // Worker sessions belong to the worker plugin; only interactive UI
      // sessions are eligible for the bridge.
      if (agent.session?.id?.startsWith('agent-society-')) continue
      if (agent.status === 'idle') return agent
    }
    return undefined
  }

  async function poll(): Promise<void> {
    if (state.busy) return
    if (activeClaim === null && idleSessionAgent() === undefined) {
      return // the human is talking; wait for the next idle round
    }
    state.busy = true
    try {
      if (activeClaim === null) {
        activeClaim = await hub.claimTask({
          actor_id: actorId,
          node_id: nodeId,
          wait_seconds: 0,
          lease_seconds: LEASE_SECONDS,
        })
      }
      if (activeClaim === null) return
      const { task, run, lease_token: leaseToken } = activeClaim
      const agent = idleSessionAgent()
      if (agent === undefined) return // conversation became active; try next poll
      await execute(agent, task, run, leaseToken)
      activeClaim = null
    } catch (error) {
      ctx.logger.warn(
        `agent-society-task-bridge failed: ${error instanceof Error ? error.message : String(error)}`,
      )
      if (activeClaim !== null) {
        const { task, run, lease_token: leaseToken } = activeClaim
        try {
          await hub.updateRun(run.run_id, {
            status: 'failed',
            error: `task-bridge failure: ${error instanceof Error ? error.message : String(error)}`,
          })
          await hub.updateTask(task.task_id, {
            run_id: run.run_id,
            lease_token: leaseToken,
            status: 'failed',
            message: 'task-bridge execution failed',
          })
        } catch {
          // Best effort; a terminal lease makes the task reclaimable again.
        }
        activeClaim = null
      }
    } finally {
      state.busy = false
    }
  }

  /** Run one claimed task in the given UI agent's conversation. */
  async function execute(
    agent: Agent,
    task: HubClaim['task'],
    run: HubClaim['run'],
    leaseToken: string,
  ): Promise<void> {
    const toolPolicy = 'full'
    const sessionId = agent.session.id
    const sessionMode = 'continuous'
    await hub.updateTask(task.task_id, {
      run_id: run.run_id,
      lease_token: leaseToken,
      status: 'working',
      message: 'DeepSeek Harness UI session active',
      partial_result: {
        phase: 'started',
        dsh_session_id: sessionId,
        started_at: Date.now(),
      },
    })
    await hub.updateRun(run.run_id, {
      status: 'active',
      result: {
        dsh_session_id: sessionId,
        dsh_session_mode: sessionMode,
        dsh_session_reused: true,
        dsh_tool_policy: toolPolicy,
        adapter: 'agent-society-dsh-plugin',
      },
    })
    ctx.logger.info(
      `agent-society-task-bridge executing ${task.task_id} in session ${sessionId}`,
    )

    agent.followup(
      createUserMessage({
        content: [{
          type: 'text',
          text: TASK_PROMPT(task, run.run_id, workspaceRoot, toolPolicy),
        }],
        source: { kind: 'user' },
      }),
    )
    await agent.whenIdle()

    const events = agent.session.events
    let resultText = ''
    try {
      resultText = lastAssistantText(events)
    } catch {
      // A session without an assistant message still completes with ''.
    }
    const toolCount = events.filter((event) => event?.type === 'tool/call').length
    const messageCount = events.filter(
      (event) =>
        event?.type === 'user/message' || event?.type === 'assistant/message',
    ).length
    await hub.updateTask(task.task_id, {
      run_id: run.run_id,
      lease_token: leaseToken,
      status: 'completed',
      message: 'DeepSeek Harness UI session completed',
      result: {
        text: resultText,
        dsh_session_id: sessionId,
        dsh_session_mode: sessionMode,
        dsh_session_reused: true,
        dsh_tool_policy: toolPolicy,
        adapter: 'agent-society-dsh-plugin',
      },
    })
    ctx.logger.info(
      `agent-society-task-bridge completed ${task.task_id} in session ${sessionId}`,
    )

    // Structured task digest into shared memory (same shape as the worker).
    const digest = buildSessionDigest({
      principalId,
      sessionId,
      actorId,
      nodeId,
      taskId: task.task_id,
      runId: run.run_id,
      title: undefined,
      workspace: workspaceRoot,
      objective: task.objective,
      status: 'completed',
      resultText,
      toolCount,
      messageCount,
      createdAt: Date.now(),
    })
    await hub.appendSharedEvent(digest)
  }
}

function stableSlug(value: string): string {
  const slug = value.toLowerCase().replace(/[^a-z0-9._-]+/gu, '-')
  return slug.replace(/^-+|-+$/gu, '') || 'node'
}
