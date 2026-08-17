/**
 * AgentSociety worker plugin for DeepSeek Harness.
 *
 * In-process execution path: claims tasks from the AgentSociety Hub, drives
 * dsh agents through `ctx.agents.create()` / `ctx.agents.resume()`, applies
 * per-task tool policies, attaches durable transcripts as Hub artifacts, and
 * writes task results back to the Hub.
 */

import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/cordis-plugin-timer'
import Schema from '@deepseek-ai/schemastery'
import type { Agent, AgentHandle, AgentSetup } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionId, type Session, type SessionEvent } from '@deepseek-ai/dsh-session'
import type { SessionTitleService } from '@deepseek-ai/dsh-session-title'
import { setSandboxMode } from '@deepseek-ai/dsh-sandbox-policy'
import type {} from '@deepseek-ai/dsh-session-persistence'
import { hostname, userInfo } from 'node:os'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { basename, dirname, isAbsolute, relative, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import {
  HubClient,
  type HubArtifact,
  type HubClaim,
  type HubTask,
} from './hub-client.js'
import { buildSessionDigest, type ConsensusDigest } from './digest.js'
import {
  loadMirror,
  mergeInvocation,
  mirrorPath,
  saveMirror,
} from './directory.js'
import {
  isSelfUpdateTask,
  runPluginSelfUpdate,
  SELF_UPDATE_EXIT_CODE,
} from './self-update.js'

export const name = 'agent-society-worker'
export const inject = ['timer', 'agents', 'sessions', 'sessionPersistence']

export type ToolPolicy = 'full' | 'read_only' | 'no_tools'

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
  toolPolicy?: ToolPolicy
  selfUpdateEnabled?: boolean
  repositoryRoot?: string
  provider?: string
  model?: string
  maxTokens?: number
  /** Append consensus digests to the Hub shared memory (AGENT_SOCIETY_CONTEXT). */
  contextEnabled?: boolean
  /** Push session directory rows / invocations (default on). */
  directoryEnabled?: boolean
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
  toolPolicy: Schema.union(['full', 'read_only', 'no_tools']).required(false),
  selfUpdateEnabled: Schema.boolean().required(false),
  repositoryRoot: Schema.string().required(false),
  provider: Schema.string().required(false),
  model: Schema.string().required(false),
  maxTokens: Schema.number().min(1).required(false),
  contextEnabled: Schema.boolean().required(false),
  directoryEnabled: Schema.boolean().required(false),
})

interface ActiveAgent {
  handle: AgentHandle
  sessionId: string
  scopeKey: string
  toolPolicy: ToolPolicy
}

interface RunningTask {
  taskId: string
  runId: string
  leaseToken: string
  agent: Agent
  sessionId: string
  continuous: boolean
  toolPolicy: ToolPolicy
  title: string | undefined
  cancelled: boolean
}

const TASK_PROMPT = (
  task: HubTask,
  runId: string,
  cwd: string,
  toolPolicy: ToolPolicy,
): string => [
  'You are executing a durable task delegated through the AgentSociety Hub.',
  `Task ID: ${task.task_id}`,
  `Run ID: ${runId}`,
  `Objective: ${task.objective}`,
  `Configured workspace: ${cwd}`,
  `Tool policy: ${toolPolicy}`,
  `Structured input: ${JSON.stringify(task.input)}`,
  'Complete the objective with the currently available tools. Return a concise result suitable for the delegating agent.',
].join('\n')

/** Tool names mounted by the shipped dsh-base bundle. */
const LOCAL_TOOL_NAMES = new Set([
  'bash',
  'create_goal',
  'edit',
  'exit_plan_mode',
  'get_goal',
  'glob',
  'grep',
  'interrupt_agent',
  'job_kill',
  'job_list',
  'job_output',
  'list_agents',
  'ralph',
  'read',
  'read_image',
  'send_message',
  'skill',
  'str_replace_editor',
  'subagent',
  'subagent_fork',
  'todo_write',
  'update_goal',
  'web_search',
  'workflow',
  'write',
])

/** Local tools that remain visible under `read_only` (plus external/MCP tools). */
const READ_ONLY_TOOL_NAMES = new Set([
  'read',
  'read_image',
  'glob',
  'grep',
  'web_search',
])

/** Local tools that remain visible under `no_tools` (plus external/MCP tools). */
const NO_TOOLS_ALLOWED_TOOL_NAMES = new Set(['web_search'])

const MAX_INLINE_ARTIFACT_BYTES = 4 * 1024 * 1024

interface ToolRuntimeLike {
  schemas(): readonly { name: string }[]
  restrict(filter: {
    allow?: readonly string[]
    deny?: readonly string[]
  }): unknown
}

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

  const toolPolicy = normalizeToolPolicy(
    config.toolPolicy ?? process.env.AGENT_SOCIETY_TOOL_POLICY,
  ) ?? 'full'

  const owner = stableSlug(userInfo().username)
  const host = stableSlug(hostname())
  const selfUpdateEnabled = config.selfUpdateEnabled ?? process.env.AGENT_SELF_UPDATE !== '0'
  const repositoryRoot = resolve(
    config.repositoryRoot ??
      process.env.AGENT_SOCIETY_REPOSITORY_ROOT ??
      resolve(dirname(fileURLToPath(import.meta.url)), '..', '..'),
  )
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
    toolPolicy,
    selfUpdateEnabled,
    repositoryRoot,
    contextEnabled: config.contextEnabled ?? process.env.AGENT_SOCIETY_CONTEXT === '1',
    directoryEnabled: config.directoryEnabled ?? process.env.AGENT_SOCIETY_DIRECTORY !== '0',
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
  const progress = ctx.setInterval(() => {
    void worker.reportProgress()
  }, 15_000)
  void worker.tick()
  void worker.maintain()
  void worker.startSse()
  ctx.effect(() => () => {
    timer()
    maintenance()
    progress()
    void worker.dispose()
  })
}

class WorkerLoop {
  private readonly active = new Map<string, ActiveAgent>()
  private busy = false
  private disposed = false
  private running: RunningTask | undefined
  private selfUpdating:
    | { taskId: string; runId: string; leaseToken: string }
    | undefined
  /** True while the SSE push channel is connected (controls/cancel are
   *  event-driven then; the polling fallback below covers disconnects). */
  private sseActive = false
  private sseAbort: AbortController | undefined
  private sseBackoffMs = 1_000
  private lastPartialSnapshot: string | undefined
  /** Consensus digests awaiting upload (fire-and-forget with bounded retries). */
  private readonly pendingDigests: Array<{ digest: ConsensusDigest; attempts: number }> = []

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
      toolPolicy: ToolPolicy
      selfUpdateEnabled: boolean
      repositoryRoot: string
      contextEnabled: boolean
      directoryEnabled: boolean
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
      capabilities: [
        'dsh',
        'code',
        'hub-task',
        'push',
        'ask',
        ...(this.options.toolPolicy === 'full' ? ['workspace-write'] : []),
      ],
      metadata: {
        runtime: 'dsh',
        runtime_version: 'plugin-0.2.0',
        tool_policy: this.options.toolPolicy,
      },
    })
    await this.hub.registerNode({
      node_id: this.options.nodeId,
      actor_id: this.options.actorId,
      display_name: this.options.displayName,
      capabilities: ['filesystem', 'remote-worker'],
      metadata: {
        runtime: 'dsh',
        workspace_root: this.options.workspaceRoot,
        tool_policy: this.options.toolPolicy,
      },
    })
  }

  async tick(): Promise<void> {
    if (this.busy || this.disposed) return
    this.busy = true
    try {
      // Long-poll claim: the Hub holds the request until a matching task is
      // available (capped at 30s server-side), so dispatch latency is not
      // bound by the poll interval.
      const claim = await this.hub.claimTask({
        actor_id: this.options.actorId,
        node_id: this.options.nodeId,
        wait_seconds: Math.min(Math.max(this.options.pollSeconds, 1), 30),
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

  /**
   * Keep the SSE push channel connected with backoff. While it is up,
   * controls and cancellation arrive as events (sub-second latency);
   * `maintain()` then only heartbeats and renews the task lease.
   */
  async startSse(): Promise<void> {
    while (!this.disposed) {
      const abort = new AbortController()
      this.sseAbort = abort
      try {
        await this.hub.subscribeEvents(
          this.options.nodeId,
          (event) => this.onSseEvent(event),
          { signal: abort.signal },
        )
        // Stream ended (server closed or aborted). Drop to the polling
        // fallback and try again shortly.
        this.sseActive = false
        if (this.disposed) return
        await sleep(this.sseBackoffMs)
      } catch (error) {
        if (this.disposed || abort.signal.aborted) return
        this.sseActive = false
        this.ctx.logger.warn(
          `agent-society-worker SSE disconnected (${message(error)}); polling fallback active`,
        )
        await sleep(this.sseBackoffMs)
        this.sseBackoffMs = Math.min(this.sseBackoffMs * 2, 30_000)
      }
    }
  }

  private onSseEvent(event: {
    name: string
    data: Record<string, unknown>
  }): void {
    this.sseActive = true
    this.sseBackoffMs = 1_000
    const running = this.running
    if (!running) return
    if (event.name === 'control/new') {
      if (event.data.task_id !== running.taskId) return
      void this.claimControls(running)
      return
    }
    if (event.name === 'task/cancelled') {
      if (event.data.task_id !== running.taskId) return
      running.cancelled = true
      running.agent.cancel({ kind: 'hook', reason: 'hub-task-cancelled' })
      void this.markRunCancelled(running)
    }
  }

  async maintain(): Promise<void> {
    if (this.disposed) return
    try {
      await this.hub.heartbeat(this.options.nodeId)
      await this.flushDigests()
      const running = this.running
      if (!running) return
      await this.hub.updateTask(running.taskId, {
        run_id: running.runId,
        lease_token: running.leaseToken,
        status: 'working',
        message: 'DeepSeek Harness session active',
      })
      if (this.sseActive) return
      // Polling fallback while the push channel is down.
      const task = await this.hub.getTask(running.taskId)
      if (task.status === 'cancelled') {
        running.cancelled = true
        running.agent.cancel({ kind: 'hook', reason: 'hub-task-cancelled' })
        await this.markRunCancelled(running)
        return
      }
      await this.claimControls(running)
    } catch (error) {
      this.ctx.logger.warn(
        `agent-society-worker maintenance failed: ${message(error)}`,
      )
    }
  }

  private async claimControls(running: RunningTask): Promise<void> {
    try {
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
        `agent-society-worker control claim failed: ${message(error)}`,
      )
    }
  }

  /**
   * Publish a bounded progressive snapshot (phase, tool counts) as
   * `partial_result` on the Hub. Observers (`agent observe`, the web
   * dashboard) can then follow live progress without waiting for the
   * terminal result. Reports only when the snapshot actually changed.
   */
  async reportProgress(): Promise<void> {
    const running = this.running
    if (!running || this.disposed) return
    try {
      const events = running.agent.session.events
      const toolCalls = events.filter((event) => event?.type === 'tool/call')
      const lastToolEvent = toolCalls[toolCalls.length - 1]
      let lastTool: string | undefined
      if (lastToolEvent) {
        const raw = lastToolEvent.data.arguments
        try {
          const parsed: unknown = JSON.parse(raw)
          if (
            parsed !== null &&
            typeof parsed === 'object' &&
            typeof (parsed as Record<string, unknown>).name === 'string'
          ) {
            lastTool = (parsed as Record<string, unknown>).name as string
          }
        } catch {
          // Raw arguments are not JSON; keep lastTool undefined.
        }
      }
      const snapshot = JSON.stringify({
        phase: 'working',
        toolCount: toolCalls.length,
        messageCount: events.filter((event) => event?.type === 'user/message' || event?.type === 'assistant/message').length,
        lastTool,
      })
      if (snapshot === this.lastPartialSnapshot) return
      this.lastPartialSnapshot = snapshot
      await this.hub.updateTask(running.taskId, {
        run_id: running.runId,
        lease_token: running.leaseToken,
        status: 'working',
        message: 'DeepSeek Harness session active',
        partial_result: JSON.parse(snapshot) as Record<string, unknown>,
      })
    } catch (error) {
      this.ctx.logger.warn(
        `agent-society-worker progress report failed: ${message(error)}`,
      )
    }
  }

  private async execute(claim: HubClaim): Promise<void> {
    const { task, run } = claim
    if (isSelfUpdateTask(task)) {
      await this.executeSelfUpdate(claim)
      return
    }
    let cwd: string
    try {
      cwd = this.taskWorkspace(task)
    } catch (error) {
      await this.failTask(claim, message(error))
      return
    }
    const scopeKey = this.scopeKey(task, cwd)
    const continuous = this.options.sessionMode === 'continuous'
    const resetRequested = task.input.reset_worker_session === true
    const toolPolicy = taskToolPolicy(task, this.options.toolPolicy)
    let active = continuous ? this.active.get(scopeKey) : undefined
    let reused = Boolean(active)

    if (continuous) {
      if (
        active &&
        (active.toolPolicy !== toolPolicy || resetRequested)
      ) {
        await active.handle.dispose()
        this.active.delete(scopeKey)
        active = undefined
        reused = false
      }
      if (active === undefined) {
        const previous = resetRequested
          ? undefined
          : readState(this.options.statePath)[scopeKey]
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
              setup: agentSetup(toolPolicy),
            })
            active = { handle, sessionId: previous, scopeKey, toolPolicy }
            this.active.set(scopeKey, active)
            reused = true
            this.ctx.logger.info(
              `agent-society-worker resumed session ${previous} (${toolPolicy})`,
            )
          } catch (error) {
            this.ctx.logger.warn(
              `agent-society-worker resume failed, creating a new session: ${message(error)}`,
            )
          }
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
        setup: agentSetup(toolPolicy),
      })
      active = { handle, sessionId, scopeKey, toolPolicy }
      if (continuous) this.active.set(scopeKey, active)
    }

    const agent = active.handle.agent
    applySandboxMode(this.ctx, agent.session, toolPolicy)
    const title = this.applySessionTitle(agent.session, task)
    const running: RunningTask = {
      taskId: task.task_id,
      runId: run.run_id,
      leaseToken: claim.lease_token,
      agent,
      sessionId: active.sessionId,
      continuous,
      toolPolicy,
      title,
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
        dsh_tool_policy: toolPolicy,
        ...(title === undefined ? {} : { dsh_session_title: title }),
        adapter: 'agent-society-dsh-plugin',
      },
    })

    try {
      agent.followup(
        createUserMessage({
          content: [{
            type: 'text',
            text: TASK_PROMPT(task, run.run_id, cwd, toolPolicy),
          }],
          source: { kind: 'user' },
        }),
      )
      await agent.whenIdle()
      await this.flushSession(agent.session)
      if (running.cancelled) {
        this.ctx.logger.info(
          `agent-society-worker cancelled ${task.task_id} in ${active.sessionId}`,
        )
        return
      }
      const text = lastAssistantText(agent.session.events)
      const finalTitle = this.currentSessionTitle(agent.session) ?? running.title
      const transcript = await this.attachTranscript(
        task,
        run.run_id,
        agent.session,
        finalTitle,
        toolPolicy,
      )
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
          dsh_tool_policy: toolPolicy,
          ...(finalTitle === undefined ? {} : { dsh_session_title: finalTitle }),
          ...(transcript === undefined
            ? {}
            : { dsh_transcript_artifact_id: transcript.artifact_id }),
        },
      })
      this.ctx.logger.info(
        `agent-society-worker completed ${task.task_id} in ${active.sessionId}`,
      )
      if (this.options.contextEnabled) {
        this.queueDigest({
          task,
          runId: run.run_id,
          session: agent.session,
          title: finalTitle,
          cwd,
          toolPolicy,
          status: 'completed',
        })
      }
      this.queueDirectoryRow({
        task,
        runId: run.run_id,
        session: agent.session,
        title: finalTitle,
        cwd,
        toolPolicy,
        status: 'completed',
      })
    } catch (error) {
      if (running.cancelled) {
        await this.markRunCancelled(running)
      } else {
        await this.flushSession(agent.session)
        const transcript = await this.attachTranscript(
          task,
          run.run_id,
          agent.session,
          running.title,
          toolPolicy,
        )
        await this.failTask(claim, message(error), {
          sessionId: active.sessionId,
          toolPolicy,
          ...(running.title === undefined ? {} : { title: running.title }),
          ...(transcript === undefined
            ? {}
            : { transcriptArtifactId: transcript.artifact_id }),
        })
        if (this.options.contextEnabled) {
          this.queueDigest({
            task,
            runId: run.run_id,
            session: agent.session,
            title: running.title,
            cwd,
            toolPolicy,
            status: 'failed',
          })
        }
        this.queueDirectoryRow({
          task,
          runId: run.run_id,
          session: agent.session,
          title: running.title,
          cwd,
          toolPolicy,
          status: 'failed',
        })
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

  private async flushSession(session: Session): Promise<void> {
    try {
      await this.ctx.sessions.flush(session)
    } catch (error) {
      this.ctx.logger.warn(
        `agent-society-worker session flush failed: ${message(error)}`,
      )
    }
  }

  private applySessionTitle(session: Session, task: HubTask): string | undefined {
    const service = this.ctx.get('sessionTitle') as SessionTitleService | undefined
    if (!service) return undefined
    const existing = service.get(session)?.title
    if (existing) return existing
    const title = taskTitle(task)
    if (!title) return existing
    try {
      return service.rename(session, title).title
    } catch (error) {
      this.ctx.logger.warn(
        `agent-society-worker session title failed: ${message(error)}`,
      )
      return service.get(session)?.title
    }
  }

  private currentSessionTitle(session: Session): string | undefined {
    const service = this.ctx.get('sessionTitle') as SessionTitleService | undefined
    return service?.get(session)?.title
  }

  private async attachTranscript(
    task: HubTask,
    runId: string,
    session: Session,
    title: string | undefined,
    toolPolicy: ToolPolicy,
  ): Promise<HubArtifact | undefined> {
    try {
      const location = this.ctx.sessionPersistence.locate(session.header)
      if (!location || !existsSync(location.path)) return undefined
      const name = `dsh-transcript-${runId}-${basename(location.path)}`
      const metadata: Record<string, unknown> = {
        dsh_session_id: session.id,
        dsh_tool_policy: toolPolicy,
        ...(title === undefined ? {} : { dsh_session_title: title }),
      }
      let raw: string | undefined
      if (this.ctx.sessionPersistence.supportsRawArtifacts) {
        raw = (await this.ctx.sessionPersistence.readRaw(session.id))?.content
      }
      if (raw) {
        const bytes = Buffer.byteLength(raw, 'utf8')
        if (bytes > 0 && bytes <= MAX_INLINE_ARTIFACT_BYTES) {
          try {
            return await this.hub.addArtifact({
              name,
              media_type: 'application/x-ndjson',
              task_id: task.task_id,
              run_id: runId,
              created_by_actor_id: this.options.actorId,
              content_base64: Buffer.from(raw, 'utf8').toString('base64'),
              metadata,
            })
          } catch (error) {
            this.ctx.logger.warn(
              `agent-society-worker inline transcript artifact failed, attaching file reference: ${message(error)}`,
            )
          }
        }
      }
      return await this.hub.addArtifact({
        name,
        media_type: 'application/x-ndjson',
        task_id: task.task_id,
        run_id: runId,
        created_by_actor_id: this.options.actorId,
        uri: pathToFileURL(location.path).href,
        metadata,
      })
    } catch (error) {
      this.ctx.logger.warn(
        `agent-society-worker transcript artifact failed: ${message(error)}`,
      )
      return undefined
    }
  }

  /**
   * Enqueue a deterministic consensus digest for this task run. Uploads are
   * fire-and-forget: failure never blocks task completion, and the idempotent
   * event_id makes a later retry a no-op on the Hub.
   */
  private queueDigest(options: {
    task: HubTask
    runId: string
    session: Session
    title: string | undefined
    cwd: string
    toolPolicy: ToolPolicy
    status: 'completed' | 'failed'
  }): void {
    const events = options.session.events
    const toolCount = events.filter((event) => event?.type === 'tool/call').length
    const messageCount = events.filter(
      (event) => event?.type === 'user/message' || event?.type === 'assistant/message',
    ).length
    let resultText = ''
    try {
      resultText = lastAssistantText(events)
    } catch {
      // A session without any assistant message still gets a digest.
    }
    const digest = buildSessionDigest({
      principalId: this.options.principalId,
      sessionId: options.session.id,
      actorId: this.options.actorId,
      nodeId: this.options.nodeId,
      taskId: options.task.task_id,
      runId: options.runId,
      title: options.title,
      workspace: options.cwd,
      objective: options.task.objective,
      status: options.status,
      resultText,
      toolCount,
      messageCount,
      createdAt: Date.now(),
    })
    this.pendingDigests.push({ digest, attempts: 0 })
    void this.flushDigests()
  }

  /** Upload queued digests; bounded retries, failures are logged only. */
  private async flushDigests(): Promise<void> {
    if (this.pendingDigests.length === 0) return
    const remaining: Array<{ digest: ConsensusDigest; attempts: number }> = []
    for (const pending of this.pendingDigests) {
      try {
        await this.hub.appendSharedEvent(pending.digest)
      } catch (error) {
        if (pending.attempts < 3) {
          remaining.push({ digest: pending.digest, attempts: pending.attempts + 1 })
        } else {
          this.ctx.logger.warn(
            `agent-society-worker digest upload dropped for ${pending.digest.session_id}: ${message(error)}`,
          )
        }
      }
    }
    this.pendingDigests.length = 0
    this.pendingDigests.push(...remaining)
  }

  /**
   * Merge the finished run into the local directory mirror and push the row
   * to the Hub (fire-and-forget; failures are logged only).
   */
  private queueDirectoryRow(options: {
    task: HubTask
    runId: string
    session: Session
    title: string | undefined
    cwd: string
    toolPolicy: ToolPolicy
    status: 'completed' | 'failed'
  }): void {
    if (!this.options.directoryEnabled) return
    const invocation = {
      task_id: options.task.task_id,
      run_id: options.runId,
      objective: options.task.objective.slice(0, 500),
      status: options.status,
      at: Date.now(),
    }
    const path = mirrorPath(dirname(this.options.statePath))
    const mirror = loadMirror(path)
    const merged = mergeInvocation({
      row: mirror.rows[options.session.id],
      sessionId: options.session.id,
      workspace: options.cwd,
      title: options.title,
      sessionMode: this.options.sessionMode,
      toolPolicy: options.toolPolicy,
      actorId: this.options.actorId,
      invocation,
    })
    mirror.rows[options.session.id] = merged
    saveMirror(path, mirror)
    void this.hub
      .upsertDirectoryRow({
        session_id: options.session.id,
        row: merged,
        principal_id: this.options.principalId,
        actor_id: this.options.actorId,
        node_id: this.options.nodeId,
      })
      .catch((error: unknown) => {
        this.ctx.logger.warn(
          `agent-society-worker directory upsert failed: ${message(error)}`,
        )
      })
  }

  private async executeSelfUpdate(claim: HubClaim): Promise<void> {
    const { task, run } = claim
    this.selfUpdating = {
      taskId: task.task_id,
      runId: run.run_id,
      leaseToken: claim.lease_token,
    }
    try {
      this.ctx.logger.info(`agent-society-worker self-update ${task.task_id}`)
      await this.hub.updateTask(task.task_id, {
        run_id: run.run_id,
        lease_token: claim.lease_token,
        status: 'working',
        message: 'Self-update starting',
      })
      await this.hub.updateRun(run.run_id, {
        status: 'active',
        result: {
          adapter: 'agent-society-dsh-plugin',
          action: 'self_update',
        },
      })
      const report = await runPluginSelfUpdate(task, {
        repositoryRoot: this.options.repositoryRoot,
        enabled: this.options.selfUpdateEnabled,
        nodePath: process.execPath,
      })
      const text = report.steps.join('\n')
      await this.hub.updateTask(task.task_id, {
        run_id: run.run_id,
        lease_token: claim.lease_token,
        status: 'completed',
        message: report.needsRestart
          ? 'Self-update applied; dsh worker restarting'
          : 'Self-update: already up to date',
        result: {
          text,
          before: report.before,
          after: report.after,
          updated: report.updated,
          needs_restart: report.needsRestart,
          adapter: 'agent-society-dsh-plugin',
        },
      })
      this.ctx.logger.info(
        `agent-society-worker self-update ${report.needsRestart ? 'applied' : 'already up to date'}`,
      )
      if (report.needsRestart) {
        setTimeout(() => {
          process.exit(SELF_UPDATE_EXIT_CODE)
        }, 300)
      }
    } catch (error) {
      await this.failTask(claim, message(error))
    } finally {
      this.selfUpdating = undefined
    }
  }

  private async markRunCancelled(running: RunningTask): Promise<void> {
    try {
      await this.hub.updateRun(running.runId, {
        status: 'cancelled',
        error: 'cancelled by Hub',
        result: {
          dsh_session_id: running.sessionId,
          dsh_tool_policy: running.toolPolicy,
          ...(running.title === undefined ? {} : { dsh_session_title: running.title }),
        },
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
    details: {
      sessionId?: string
      toolPolicy?: ToolPolicy
      title?: string
      transcriptArtifactId?: string
    } = {},
  ): Promise<void> {
    const { task, run } = claim
    const result: Record<string, unknown> = {
      adapter: 'agent-society-dsh-plugin',
      ...(details.sessionId ? { dsh_session_id: details.sessionId } : {}),
      ...(details.toolPolicy === undefined ? {} : { dsh_tool_policy: details.toolPolicy }),
      ...(details.title === undefined ? {} : { dsh_session_title: details.title }),
      ...(details.transcriptArtifactId === undefined
        ? {}
        : { dsh_transcript_artifact_id: details.transcriptArtifactId }),
    }
    try {
      await this.hub.updateTask(task.task_id, {
        run_id: run.run_id,
        lease_token: claim.lease_token,
        status: 'failed',
        message: messageText,
        result,
      })
      await this.hub.updateRun(run.run_id, {
        status: 'failed',
        error: messageText,
        result,
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
    this.sseAbort?.abort()
    for (const active of this.active.values()) {
      await active.handle.dispose()
    }
    this.active.clear()
  }
}

interface SandboxPolicyLike {
  overrideOf(session: Session): string | undefined
}

function applySandboxMode(
  ctx: Context,
  session: Session,
  toolPolicy: ToolPolicy,
): void {
  const mode = toolPolicy === 'full' ? 'workspace-write' : 'read-only'
  const sandboxPolicy = ctx.get('sandboxPolicy') as SandboxPolicyLike | undefined
  if (sandboxPolicy?.overrideOf(session) === mode) return
  setSandboxMode(session, mode)
}

function agentSetup(toolPolicy: ToolPolicy): AgentSetup {
  return (agentCtx) => {
    applyToolPolicy(agentCtx, toolPolicy)
  }
}

function applyToolPolicy(agentCtx: Context, toolPolicy: ToolPolicy): void {
  if (toolPolicy === 'full') return
  const tools = agentCtx.get('tools') as ToolRuntimeLike | undefined
  if (!tools || typeof tools.restrict !== 'function') return
  const names = [...new Set(tools.schemas().map((schema) => schema.name))]
  const allowed = toolPolicy === 'read_only'
    ? READ_ONLY_TOOL_NAMES
    : NO_TOOLS_ALLOWED_TOOL_NAMES
  const keep = names.filter(
    (name) => allowed.has(name) || !LOCAL_TOOL_NAMES.has(name),
  )
  if (keep.length > 0) {
    tools.restrict({ allow: keep })
    return
  }
  const deny = names.filter((name) => LOCAL_TOOL_NAMES.has(name))
  if (deny.length > 0) tools.restrict({ deny })
}

function taskToolPolicy(task: HubTask, fallback: ToolPolicy): ToolPolicy {
  return normalizeToolPolicy(task.input.tool_policy) ?? fallback
}

function normalizeToolPolicy(value: unknown): ToolPolicy | undefined {
  if (value === 'full' || value === 'read_only' || value === 'no_tools') {
    return value
  }
  return undefined
}

function taskTitle(task: HubTask): string | undefined {
  const requested = task.input.title
  if (typeof requested === 'string' && requested.trim()) {
    return truncateTitleUtf8(requested.trim())
  }
  const objective = task.objective.replace(/\s+/gu, ' ').trim()
  if (!objective) return `Hub task ${task.task_id.slice(0, 8)}`
  return truncateTitleUtf8(objective)
}

function truncateTitleUtf8(value: string, maxBytes = 80): string {
  if (Buffer.byteLength(value, 'utf8') <= maxBytes) return value
  let result = ''
  let bytes = 0
  for (const character of value) {
    const size = Buffer.byteLength(character, 'utf8')
    if (bytes + size > maxBytes) break
    result += character
    bytes += size
  }
  return result.trim()
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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
