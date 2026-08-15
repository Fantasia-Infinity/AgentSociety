/**
 * AgentSociety worker plugin for DeepSeek Harness.
 *
 * This is the first in-process dsh execution path: it claims tasks from the
 * AgentSociety Hub, drives dsh agents through `ctx.agents.create()` /
 * `ctx.agents.resume()`, and writes task results back to the Hub.
 */

import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/cordis-plugin-timer'
import Schema from '@deepseek-ai/schemastery'
import type { Agent, AgentHandle } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionId, type SessionEvent } from '@deepseek-ai/dsh-session'
import type {} from '@deepseek-ai/dsh-session-persistence'
import { hostname, userInfo } from 'node:os'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { dirname, isAbsolute, relative, resolve } from 'node:path'
import { HubClient, type HubClaim, type HubTask } from './hub-client.js'

export const name = 'agent-society-worker'
export const inject = ['timer', 'agents', 'sessionPersistence']

export interface Config {
  hubUrl?: string
  hubTokenEnv?: string
  pollSeconds?: number
  leaseSeconds?: number
  actorId?: string
  nodeId?: string
  principalId?: string
  displayName?: string
  workspaceRoot?: string
  sessionMode?: 'per_task' | 'continuous'
  provider?: string
  model?: string
  maxTokens?: number
}

export const Config: Schema<Config> = Schema.object({
  hubUrl: Schema.string().required(false),
  hubTokenEnv: Schema.string().required(false),
  pollSeconds: Schema.number().min(1).default(20),
  leaseSeconds: Schema.number().min(30).default(300),
  actorId: Schema.string().required(false),
  nodeId: Schema.string().required(false),
  principalId: Schema.string().required(false),
  displayName: Schema.string().required(false),
  workspaceRoot: Schema.string().required(false),
  sessionMode: Schema.union(['per_task', 'continuous']).default('per_task'),
  provider: Schema.string().required(false),
  model: Schema.string().required(false),
  maxTokens: Schema.number().min(1).required(false),
})

interface ActiveAgent {
  handle: AgentHandle
  sessionId: string
  scopeKey: string
}

interface RunningTask {
  taskId: string
  runId: string
  leaseToken: string
  agent: Agent
  sessionId: string
  continuous: boolean
  cancelled: boolean
}

const TASK_PROMPT = (task: HubTask, runId: string, cwd: string): string => [
  'You are executing a durable task delegated through the AgentSociety Hub.',
  `Task ID: ${task.task_id}`,
  `Run ID: ${runId}`,
  `Objective: ${task.objective}`,
  `Configured workspace: ${cwd}`,
  `Structured input: ${JSON.stringify(task.input)}`,
  'Complete the objective with the currently available tools. Return a concise result suitable for the delegating agent.',
].join('\n')

export async function apply(ctx: Context, config: Config): Promise<void> {
  const hubUrl = config.hubUrl ?? process.env.AGENT_SOCIETY_HUB_URL?.trim()
  const tokenEnv = config.hubTokenEnv ?? 'AGENT_SOCIETY_HUB_TOKEN'
  const hubToken = process.env[tokenEnv]?.trim()
  if (!hubUrl || !hubToken) {
    ctx.logger.warn(
      'agent-society-worker: AGENT_SOCIETY_HUB_URL and AGENT_SOCIETY_HUB_TOKEN are required; worker stays idle',
    )
    return
  }

  const owner = stableSlug(userInfo().username)
  const host = stableSlug(hostname())
  const identity = {
    principalId: config.principalId ?? `human-${owner}`,
    actorId: config.actorId ?? `agent-society-${host}`,
    nodeId: config.nodeId ?? host,
    displayName: config.displayName ?? `AgentSociety dsh worker on ${hostname()}`,
    workspaceRoot: resolve(config.workspaceRoot ?? process.cwd()),
  }
  const hub = new HubClient(hubUrl, hubToken)
  const worker = new WorkerLoop(ctx, hub, {
    ...identity,
    pollSeconds: config.pollSeconds ?? 20,
    leaseSeconds: config.leaseSeconds ?? 300,
    sessionMode: config.sessionMode ?? 'per_task',
    provider: config.provider ?? 'deepseek-official',
    model: config.model ?? process.env.DSH_MODEL ?? 'deepseek-v4-flash',
    maxTokens: config.maxTokens,
    statePath: resolve(
      process.env.DSH_HOME ?? `${process.env.HOME ?? '.'}/.dsh`,
      'agent-society-worker-sessions.json',
    ),
  })

  await worker.register()
  const timer = ctx.setInterval(() => {
    void worker.tick()
  }, worker.pollSeconds * 1_000)
  const maintenance = ctx.setInterval(() => {
    void worker.maintain()
  }, 2_000)
  void worker.tick()
  void worker.maintain()
  ctx.effect(() => () => {
    timer()
    maintenance()
    void worker.dispose()
  })
}

class WorkerLoop {
  private readonly active = new Map<string, ActiveAgent>()
  private busy = false
  private disposed = false
  private running: RunningTask | undefined

  constructor(
    private readonly ctx: Context,
    private readonly hub: HubClient,
    readonly options: {
      principalId: string
      actorId: string
      nodeId: string
      displayName: string
      workspaceRoot: string
      pollSeconds: number
      leaseSeconds: number
      sessionMode: 'per_task' | 'continuous'
      provider: string
      model: string
      maxTokens: number | undefined
      statePath: string
    },
  ) {}

  get pollSeconds(): number {
    return this.options.pollSeconds
  }

  async register(): Promise<void> {
    await this.hub.registerPrincipal({
      principal_id: this.options.principalId,
      kind: 'human',
      display_name: this.options.displayName,
    })
    await this.hub.registerActor({
      actor_id: this.options.actorId,
      principal_id: this.options.principalId,
      kind: 'agent',
      display_name: this.options.displayName,
      capabilities: ['dsh', 'code', 'hub-task'],
      metadata: { runtime: 'dsh', runtime_version: 'plugin-0.1.0' },
    })
    await this.hub.registerNode({
      node_id: this.options.nodeId,
      actor_id: this.options.actorId,
      display_name: this.options.displayName,
      capabilities: ['filesystem', 'remote-worker'],
      metadata: {
        runtime: 'dsh',
        workspace_root: this.options.workspaceRoot,
      },
    })
  }

  async tick(): Promise<void> {
    if (this.busy || this.disposed) return
    this.busy = true
    try {
      const claim = await this.hub.claimTask({
        actor_id: this.options.actorId,
        node_id: this.options.nodeId,
        wait_seconds: 0,
        lease_seconds: this.options.leaseSeconds,
      })
      if (claim) await this.execute(claim)
    } catch (error) {
      this.ctx.logger.warn(
        `agent-society-worker tick failed: ${message(error)}`,
      )
    } finally {
      this.busy = false
    }
  }

  async maintain(): Promise<void> {
    if (this.disposed) return
    try {
      await this.hub.heartbeat(this.options.nodeId)
      const running = this.running
      if (!running) return
      await this.hub.updateTask(running.taskId, {
        run_id: running.runId,
        lease_token: running.leaseToken,
        status: 'working',
        message: 'DeepSeek Harness session active',
      })
      const task = await this.hub.getTask(running.taskId)
      if (task.status === 'cancelled') {
        running.cancelled = true
        running.agent.cancel({ kind: 'hook', reason: 'hub-task-cancelled' })
        await this.markRunCancelled(running)
        return
      }
      const controls = await this.hub.claimTaskControls(running.taskId, {
        run_id: running.runId,
        lease_token: running.leaseToken,
      })
      for (const control of controls) {
        const message = createUserMessage({
          content: [{ type: 'text', text: control.message }],
          source: { kind: 'user' },
        })
        if (control.kind === 'steer') {
          running.agent.steer(message)
        } else {
          running.agent.followup(message)
        }
        await this.hub.acknowledgeTaskControl(
          running.taskId,
          control.control_id,
          {
            run_id: running.runId,
            lease_token: control.lease_token,
          },
        )
        this.ctx.logger.info(
          `agent-society-worker applied ${control.kind} control ${control.control_id}`,
        )
      }
    } catch (error) {
      this.ctx.logger.warn(
        `agent-society-worker maintenance failed: ${message(error)}`,
      )
    }
  }

  private async execute(claim: HubClaim): Promise<void> {
    const { task, run } = claim
    let cwd: string
    try {
      cwd = this.taskWorkspace(task)
    } catch (error) {
      await this.failTask(claim, message(error))
      return
    }
    const scopeKey = this.scopeKey(task, cwd)
    const continuous = this.options.sessionMode === 'continuous'
    let active = continuous ? this.active.get(scopeKey) : undefined
    let reused = Boolean(active)

    if (continuous && active === undefined) {
      const previous = readState(this.options.statePath)[scopeKey]
      if (previous) {
        try {
          const handle = await this.ctx.agents.resume({
            resumeSessionId: SessionId(previous),
            agentOptions: {
              provider: this.options.provider,
              model: this.options.model,
              ...(this.options.maxTokens === undefined
                ? {}
                : { maxTokens: this.options.maxTokens }),
            },
          })
          active = { handle, sessionId: previous, scopeKey }
          this.active.set(scopeKey, active)
          reused = true
          this.ctx.logger.info(
            `agent-society-worker resumed session ${previous}`,
          )
        } catch (error) {
          this.ctx.logger.warn(
            `agent-society-worker resume failed, creating a new session: ${message(error)}`,
          )
        }
      }
    }

    if (active === undefined) {
      const sessionId = `agent-society-${randomUUID().replaceAll('-', '')}`
      const handle = await this.ctx.agents.create({
        sessionId: SessionId(sessionId),
        agentOptions: {
          provider: this.options.provider,
          model: this.options.model,
          ...(this.options.maxTokens === undefined
            ? {}
            : { maxTokens: this.options.maxTokens }),
        },
        meta: { cwd },
      })
      active = { handle, sessionId, scopeKey }
      if (continuous) this.active.set(scopeKey, active)
    }

    const agent = active.handle.agent
    const running: RunningTask = {
      taskId: task.task_id,
      runId: run.run_id,
      leaseToken: claim.lease_token,
      agent,
      sessionId: active.sessionId,
      continuous,
      cancelled: false,
    }
    this.running = running
    await this.hub.updateTask(task.task_id, {
      run_id: run.run_id,
      lease_token: claim.lease_token,
      status: 'working',
      message: 'DeepSeek Harness session active',
    })
    await this.hub.updateRun(run.run_id, {
      status: 'active',
      result: {
        dsh_session_id: active.sessionId,
        dsh_session_mode: continuous ? 'continuous' : 'per_task',
        dsh_session_reused: reused,
        adapter: 'agent-society-dsh-plugin',
      },
    })

    try {
      agent.followup(
        createUserMessage({
          content: [{ type: 'text', text: TASK_PROMPT(task, run.run_id, cwd) }],
          source: { kind: 'user' },
        }),
      )
      await agent.whenIdle()
      if (running.cancelled) {
        this.ctx.logger.info(
          `agent-society-worker cancelled ${task.task_id} in ${active.sessionId}`,
        )
        return
      }
      const text = lastAssistantText(agent.session.events)
      await this.hub.updateTask(task.task_id, {
        run_id: run.run_id,
        lease_token: claim.lease_token,
        status: 'completed',
        message: 'DeepSeek Harness session completed',
        result: {
          text,
          provider: this.options.provider,
          model: this.options.model,
          dsh_session_id: active.sessionId,
          dsh_session_mode: continuous ? 'continuous' : 'per_task',
          dsh_session_reused: reused,
        },
      })
      this.ctx.logger.info(
        `agent-society-worker completed ${task.task_id} in ${active.sessionId}`,
      )
    } catch (error) {
      if (running.cancelled) {
        await this.markRunCancelled(running)
      } else {
        await this.failTask(claim, message(error), active.sessionId)
      }
    } finally {
      this.running = undefined
      if (continuous) {
        if (!running.cancelled) {
          writeState(this.options.statePath, {
            ...readState(this.options.statePath),
            [scopeKey]: active.sessionId,
          })
        }
      } else {
        await active.handle.dispose()
      }
    }
  }

  private async markRunCancelled(running: RunningTask): Promise<void> {
    try {
      await this.hub.updateRun(running.runId, {
        status: 'cancelled',
        error: 'cancelled by Hub',
        result: { dsh_session_id: running.sessionId },
      })
    } catch (error) {
      this.ctx.logger.warn(
        `agent-society-worker could not mark run ${running.runId} cancelled: ${message(error)}`,
      )
    }
  }

  private async failTask(
    claim: HubClaim,
    messageText: string,
    sessionId?: string,
  ): Promise<void> {
    const { task, run } = claim
    try {
      await this.hub.updateTask(task.task_id, {
        run_id: run.run_id,
        lease_token: claim.lease_token,
        status: 'failed',
        message: messageText,
        result: {
          ...(sessionId ? { dsh_session_id: sessionId } : {}),
          adapter: 'agent-society-dsh-plugin',
        },
      })
      await this.hub.updateRun(run.run_id, {
        status: 'failed',
        error: messageText,
        result: { dsh_session_id: sessionId ?? '' },
      })
    } catch (error) {
      this.ctx.logger.warn(
        `agent-society-worker could not report failure for ${task.task_id}: ${message(error)}`,
      )
    }
  }

  private taskWorkspace(task: HubTask): string {
    const raw = task.input.workspace
    const requested = typeof raw === 'string' && raw.trim() ? raw.trim() : '.'
    const candidate = resolve(this.options.workspaceRoot, requested)
    const root = this.options.workspaceRoot
    const relativePath = relative(root, candidate)
    if (
      candidate !== root &&
      (relativePath === '' ||
        relativePath.startsWith('..') ||
        isAbsolute(relativePath))
    ) {
      throw new Error(`task workspace escapes AGENT_WORKSPACE_ROOT: ${requested}`)
    }
    if (!existsSync(candidate)) {
      throw new Error(`task workspace does not exist: ${candidate}`)
    }
    return candidate
  }

  private scopeKey(task: HubTask, cwd: string): string {
    return [task.principal_id, cwd].join('\u0000')
  }

  async dispose(): Promise<void> {
    this.disposed = true
    for (const active of this.active.values()) {
      await active.handle.dispose()
    }
    this.active.clear()
  }
}

function lastAssistantText(events: readonly SessionEvent[]): string {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (!event || event.type !== 'assistant/message') continue
    const content = event.data.message.content
    const text = content
      .filter(
        (block): block is Extract<(typeof content)[number], { type: 'text' }> =>
          block.type === 'text',
      )
      .map((block) => block.text)
      .join('')
      .trim()
    if (text) return text
  }
  throw new Error('DeepSeek Harness session ended without an assistant message')
}

function readState(path: string): Record<string, string> {
  try {
    const value = JSON.parse(readFileSync(path, 'utf8')) as unknown
    if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
      const record = value as Record<string, unknown>
      const result: Record<string, string> = {}
      for (const [key, sessionId] of Object.entries(record)) {
        if (typeof sessionId === 'string' && sessionId) result[key] = sessionId
      }
      return result
    }
  } catch {
    // Missing or partial state is a normal first run.
  }
  return {}
}

function writeState(path: string, state: Record<string, string>): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
  writeFileSync(path, `${JSON.stringify(state, null, 2)}\n`, {
    encoding: 'utf8',
    mode: 0o600,
  })
}

function stableSlug(value: string): string {
  const slug = value.toLowerCase().replace(/[^a-z0-9._-]+/gu, '-')
  return slug.replace(/^-+|-+$/gu, '') || 'node'
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
