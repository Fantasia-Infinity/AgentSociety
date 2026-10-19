/**
 * Interactive-session consensus digest watcher.
 *
 * Runs inside every hub-connected dsh process (TUI / Web / worker). For
 * each LIVE session in this process (the ones a human is actually talking
 * to), it detects the end of an interaction round — new messages followed
 * by an idle gap — and appends a consensus digest, summarized by the LLM
 * by default (KV-cache-friendly: session-derived prefix + trailing
 * instruction, see summarizer.ts) with a deterministic fallback.
 *
 * Anti-repetition guarantees (no LLM calls unless warranted):
 * - zero new content => zero LLM calls (pure count comparison);
 * - one digest per round: a persisted watermark per session
 *   (~/.dsh/agent-society-digest-state.json, 0600) survives restarts;
 * - in-flight guard prevents re-entry; bounded retries (3) then give up;
 * - idempotent event_id (principal|consensus|digest|session|round count),
 *   so even a cross-process duplicate cannot duplicate the Hub entry;
 * - Worker sessions are treated like any other session: the unified
 *   session model means a continuous worker conversation is summarized
 *   per idle round exactly like a UI session (the worker task path adds
 *   its own structured task digests via queueDigest, at a different
 *   granularity, without duplicating the LLM summarization).
 */

import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/cordis-plugin-timer'
import Schema from '@deepseek-ai/schemastery'
import { createHash, randomUUID } from 'node:crypto'
import { homedir, hostname, userInfo } from 'node:os'
import {
  closeSync,
  mkdirSync,
  openSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
  writeSync,
} from 'node:fs'
import { dirname, resolve } from 'node:path'

import type { EpochHeader } from '@deepseek-ai/dsh-session'
import { HubClient } from './hub-client.js'
import { loadProjectionActivity } from './directory.js'
import { buildSessionDigest } from './digest.js'
import {
  DEFAULT_SUMMARY_MAX_TOKENS,
  SUMMARY_MAX_TOKENS_HARD_LIMIT,
} from './summarizer.js'
import {
  deterministicSummary,
  summarizeLiveSession,
  summarizeStandalone,
  type SummaryFields,
} from './summarizer.js'
import { recordAgentSocietyMetrics } from './context-metrics.js'

export const name = 'agent-society-session-digest'
export const inject = ['timer']

export interface Config {
  hubUrl?: string
  hubTokenEnv?: string
  principalId?: string
  actorId?: string
  nodeId?: string
  workspaceRoot?: string
  provider?: string
  model?: string
  maxTokens?: number
  pollSeconds?: number
  idleSeconds?: number
  /** Summarize with the LLM (default on); 0 falls back to deterministic. */
  summarize?: boolean
  /** Allow digest writes at all (AGENT_SOCIETY_CONTEXT). */
  contextEnabled?: boolean
}

export const Config: Schema<Config> = Schema.object({
  hubUrl: Schema.string().required(false),
  hubTokenEnv: Schema.string().required(false),
  principalId: Schema.string().required(false),
  actorId: Schema.string().required(false),
  nodeId: Schema.string().required(false),
  workspaceRoot: Schema.string().required(false),
  provider: Schema.string().required(false),
  model: Schema.string().required(false),
  maxTokens: Schema.number().min(1).required(false),
  pollSeconds: Schema.number().min(5).default(15),
  idleSeconds: Schema.number().min(10).default(60),
  summarize: Schema.boolean().required(false),
  contextEnabled: Schema.boolean().required(false),
})

interface SessionEventLike {
  readonly type?: string
  readonly time?: number
  readonly data?: unknown
}

type SessionEventList = ReadonlyArray<SessionEventLike | undefined>

interface SessionLike {
  readonly id: string
  readonly snapshotEvents?: () => SessionEventList
  /** Compatibility face for older dsh releases and test doubles. */
  readonly events?: SessionEventList
}

interface LiveSessionLike extends SessionLike {
  requestHeader(): EpochHeader | undefined
  deriveMessages(): unknown[]
}

interface SessionsLike {
  list(): SessionLike[]
}

interface PersistenceHeaderLike {
  readonly id: string
  readonly createdAt?: number
  readonly cwd?: string
}

interface PersistenceSnapshotLike {
  readonly header?: PersistenceHeaderLike
  /** Compatibility face for the pre-0.1.5 list result. */
  readonly id?: string
  readonly createdAt?: number
  readonly cwd?: string
}

interface PersistenceReadHandleLike {
  readonly header?: PersistenceHeaderLike
  read(): Promise<{ events: SessionEventList }>
  close(): Promise<void>
}

interface PersistenceLike {
  list(): Promise<ReadonlyArray<PersistenceSnapshotLike>>
  open?(id: string, access: 'read'): Promise<PersistenceReadHandleLike>
  /** Compatibility face for the pre-0.1.5 persistence API. */
  inspect?(id: string): Promise<{ events: SessionEventList }>
}

interface DigestState {
  [sessionId: string]: { count: number; lastPromptAt?: number; digestAt: number }
}

const MAX_ATTEMPTS = 3
export const DIGEST_LEASE_SECONDS = 90

export function apply(ctx: Context, config: Config): void {
  const hubUrl = config.hubUrl ?? process.env.AGENT_SOCIETY_HUB_URL?.trim()
  const tokenEnv = config.hubTokenEnv ?? 'AGENT_SOCIETY_HUB_TOKEN'
  const hubToken = process.env[tokenEnv]?.trim()
  const contextEnabled =
    config.contextEnabled ?? process.env.AGENT_SOCIETY_CONTEXT !== '0'
  if (!hubUrl || !hubToken || !contextEnabled) {
    ctx.logger.info(
      `agent-society-session-digest idle (hub=${Boolean(hubUrl)} token=${Boolean(hubToken)} context=${contextEnabled})`,
    )
    return
  }
  const owner = stableSlug(userInfo().username)
  const host = stableSlug(hostname())
  const principalId = config.principalId ?? `human-${owner}`
  const actorId = config.actorId ?? `agent-society-${host}`
  const nodeId = config.nodeId ?? host
  const workspaceRoot = resolve(config.workspaceRoot ?? process.cwd())
  const provider = config.provider ?? process.env.AGENT_SOCIETY_PROVIDER ?? 'deepseek-official'
  const model =
    config.model ??
    process.env.AGENT_SOCIETY_MODEL ??
    process.env.AGENT_DSH_MODEL ??
    process.env.DSH_MODEL ??
    process.env.AGENT_REMOTE_MODEL ??
    'deepseek-flash'
  const maxTokens = Math.min(
    config.maxTokens ?? DEFAULT_SUMMARY_MAX_TOKENS,
    SUMMARY_MAX_TOKENS_HARD_LIMIT,
  )
  const pollSeconds = config.pollSeconds ?? 15
  const idleSeconds = config.idleSeconds ?? 60
  const summarize =
    config.summarize ?? process.env.AGENT_SOCIETY_CONTEXT_SUMMARIZE !== '0'

  ctx.logger.warn(
    `agent-society-session-digest active (hub=${hubUrl}, summarize=${summarize}, poll=${pollSeconds}s, idle=${idleSeconds}s)`,
  )

  const hub = new HubClient(hubUrl, hubToken)
  const dshHome = resolve(process.env.DSH_HOME?.trim() || resolve(homedir(), '.dsh'))
  const statePath = resolve(dshHome, 'agent-society-digest-state.json')
  const state: DigestState = loadState(statePath)
  const inFlight = new Set<string>()
  const attempts = new Map<string, number>()

  const timer = ctx.setInterval(() => {
    void tick()
  }, pollSeconds * 1_000)
  ctx.effect(() => () => timer())
  void tick()

  async function tick(): Promise<void> {
    const sessions = ctx.get('sessions') as SessionsLike | undefined
    const persistence = ctx.get('sessionPersistence') as PersistenceLike | undefined
    const now = Date.now()
    const liveIds = new Set<string>()
    ctx.logger.debug(
      `agent-society-session-digest tick (sessions=${Boolean(sessions)}, persistence=${Boolean(persistence)})`,
    )

    // Path 1: live sessions in THIS process (best KV-cache behaviour:
    // session-derived prefix + trailing instruction).
    if (sessions && typeof sessions.list === 'function') {
      for (const session of sessions.list()) {
        if (!session || typeof session.id !== 'string') continue
        liveIds.add(session.id)
        await handleRound(session.id, sessionEvents(session), { live: true })
      }
    }

    // Path 2: persisted sessions not live here (the web may recreate agent
    // fibers per turn, so between turns the store can be empty). Uses the
    // projection cache's lastPromptAt as the activity watermark and
    // standalone summarization (no session context copy). When the cache has
    // no lastPromptAt (the TUI never writes that field — only the web
    // surface does), fall back to the persisted event tail so a running
    // worker can backfill digests for interactive sessions that were closed
    // before their idle gap was observed in-process.
    if (persistence && typeof persistence.list === 'function') {
      try {
        const headers = await persistence.list()
        const activity = loadProjectionActivity(
          process.env.DSH_HOME?.trim() || resolve(homedir(), '.dsh'),
        )
        for (const snapshot of headers) {
          const header = persistenceHeader(snapshot)
          if (header === undefined) continue
          if (liveIds.has(header.id)) continue
          const info = activity.get(header.id)
          const prior = state[header.id]
          let lastPromptAt = info?.lastPromptAt
          // Read the persisted tail once; it also feeds the summary below.
          let events: SessionEventList | undefined
          if (lastPromptAt === undefined) {
            events = (await readPersistedEvents(persistence, header.id))
            const lastTime = lastEventTimeMs(events)
            if (lastTime === undefined) continue
            lastPromptAt = lastTime
          }
          if (prior !== undefined && prior.lastPromptAt !== undefined && lastPromptAt <= prior.lastPromptAt) continue
          // A round is over only after an idle gap since the last prompt.
          if (now - lastPromptAt < idleSeconds * 1_000) continue
          if (inFlight.has(header.id)) continue
          if (attempts.get(header.id) ?? 0 >= MAX_ATTEMPTS) continue
          inFlight.add(header.id)
          let lease: DigestLeaseResult | undefined
          try {
            if (events === undefined) {
              events = await readPersistedEvents(persistence, header.id)
            }
            const count = messageCountOf(events)
            // A session with no real conversation messages has nothing to
            // summarize; skip instead of writing an empty consensus digest.
            if (count === 0) {
              state[header.id] = { count: 0, lastPromptAt, digestAt: now }
              saveState(statePath, state)
              continue
            }
            const roundEventId = digestEventIdForRound(principalId, header.id, count)
            if (summarize) {
              lease = tryAcquireDigestLease(
                digestLeasePath(dshHome, roundEventId),
                roundEventId,
              )
              if (lease.status === 'busy') {
                recordAgentSocietyMetrics(dshHome, {
                  digestDeduplicatedCall: true,
                })
                ctx.logger.debug(
                  `agent-society-session-digest skipped duplicate round ${count} for ${header.id} (another profile owns the lease)`,
                )
                continue
              }
              recordAgentSocietyMetrics(dshHome, { digestModelCall: true })
            }
            const summary = await summarizeFor(
              { id: header.id, events },
              count,
              { live: false, ...(info === undefined ? {} : { title: info.title }) },
            )
            const digest = buildSessionDigest(
              {
                principalId,
                sessionId: header.id,
                actorId,
                nodeId,
                title: summary.title,
                workspace: header.cwd ?? workspaceRoot,
                objective: summary.objective,
                status: 'done',
                resultText: summary.resultText,
                toolCount: summary.toolCount,
                messageCount: summary.messageCount,
                createdAt: now,
              },
              {
                eventId: roundEventId,
                summary: summary.summary,
              },
            )
            await hub.appendSharedEvent(digest)
            state[header.id] = { count, lastPromptAt, digestAt: now }
            saveState(statePath, state)
            attempts.delete(header.id)
            ctx.logger.info(
              `agent-society-session-digest wrote digest for ${header.id} (round ${count}, persisted path)`,
            )
          } catch (error) {
            attempts.set(header.id, (attempts.get(header.id) ?? 0) + 1)
            ctx.logger.warn(
              `agent-society-session-digest failed for ${header.id}: ${error instanceof Error ? error.message : String(error)}`,
            )
          } finally {
            releaseDigestLease(lease)
            inFlight.delete(header.id)
          }
        }
      } catch (error) {
        ctx.logger.warn(
          `agent-society-session-digest persistence scan failed: ${error instanceof Error ? error.message : String(error)}`,
        )
      }
    }
  }

  async function handleRound(
    sessionId: string,
    events: SessionEventList,
    options: { live: boolean },
  ): Promise<void> {
    const now = Date.now()
    // Round boundary and idempotency salt = real conversation messages only;
    // events.length grows with UI activity events (activity/status etc.) and
    // would re-trigger digests across process restarts.
    const count = messageCountOf(events)
    const prior = state[sessionId]
    if (prior !== undefined && count <= prior.count) return
    const lastEventTime = lastEventTimeMs(events)
    if (lastEventTime !== undefined && now - lastEventTime < idleSeconds * 1_000) {
      return
    }
    if (inFlight.has(sessionId)) return
    if (attempts.get(sessionId) ?? 0 >= MAX_ATTEMPTS) return
    inFlight.add(sessionId)
    let lease: DigestLeaseResult | undefined
    try {
      const roundEventId = digestEventIdForRound(principalId, sessionId, count)
      if (summarize) {
        lease = tryAcquireDigestLease(
          digestLeasePath(dshHome, roundEventId),
          roundEventId,
        )
        if (lease.status === 'busy') {
          recordAgentSocietyMetrics(dshHome, {
            digestDeduplicatedCall: true,
          })
          ctx.logger.debug(
            `agent-society-session-digest skipped duplicate round ${count} for ${sessionId} (another profile owns the lease)`,
          )
          return
        }
        recordAgentSocietyMetrics(dshHome, { digestModelCall: true })
      }
      const summary = await summarizeFor(
        { id: sessionId, events },
        count,
        { live: options.live },
      )
      const digest = buildSessionDigest(
        {
          principalId,
          sessionId,
          actorId,
          nodeId,
          title: summary.title,
          workspace: workspaceRoot,
          objective: summary.objective,
          status: 'done',
          resultText: summary.resultText,
          toolCount: summary.toolCount,
          messageCount: summary.messageCount,
          createdAt: now,
        },
        {
          eventId: roundEventId,
          summary: summary.summary,
        },
      )
      await hub.appendSharedEvent(digest)
      state[sessionId] = { count, digestAt: now }
      saveState(statePath, state)
      attempts.delete(sessionId)
      ctx.logger.info(
        `agent-society-session-digest wrote digest for ${sessionId} (round ${count})`,
      )
    } catch (error) {
      attempts.set(sessionId, (attempts.get(sessionId) ?? 0) + 1)
      ctx.logger.warn(
        `agent-society-session-digest failed for ${sessionId}: ${error instanceof Error ? error.message : String(error)}`,
      )
    } finally {
      releaseDigestLease(lease)
      inFlight.delete(sessionId)
    }
  }

  async function summarizeFor(
    session: SessionLike,
    count: number,
    options: { live: boolean; title?: string },
  ): Promise<SummaryFields & { summary: string }> {
    const title = options.title ?? sessionTitleOf(session)
    const fields: SummaryFields = { ...extractFields(session), title }
    if (!summarize) {
      return { ...fields, summary: deterministicSummary(fields) }
    }
    try {
      const live = ctx.get('sessions') as
        | { get(id: string): LiveSessionLike | undefined }
        | undefined
      const liveSession = options.live ? live?.get(session.id) : undefined
      const requestHeader = liveSession?.requestHeader()
      if (liveSession !== undefined && requestHeader !== undefined) {
        const text = await summarizeLiveSession({
          ctx,
          session: liveSession as never,
          requestHeader,
          maxTokens,
        })
        return { ...fields, summary: text }
      }
      const text = await summarizeStandalone({
        ctx,
        fields,
        provider,
        model,
        maxTokens,
      })
      return { ...fields, summary: text }
    } catch (error) {
      ctx.logger.warn(
        `agent-society-session-digest summary failed, using deterministic fallback: ${error instanceof Error ? error.message : String(error)}`,
      )
      return { ...fields, summary: deterministicSummary(fields) }
    }
  }

  function sessionTitleOf(session: SessionLike): string | undefined {
    const service = ctx.get('sessionTitle') as
      | { get(session: unknown): { title?: string } | undefined }
      | undefined
    return service?.get(session)?.title
  }

}

function sessionEvents(session: SessionLike): SessionEventList {
  return typeof session.snapshotEvents === 'function'
    ? session.snapshotEvents()
    : session.events ?? []
}

function persistenceHeader(
  snapshot: PersistenceSnapshotLike,
): PersistenceHeaderLike | undefined {
  if (snapshot.header !== undefined && typeof snapshot.header.id === 'string') {
    return snapshot.header
  }
  if (typeof snapshot.id === 'string') {
    return {
      id: snapshot.id,
      ...(snapshot.createdAt === undefined ? {} : { createdAt: snapshot.createdAt }),
      ...(snapshot.cwd === undefined ? {} : { cwd: snapshot.cwd }),
    }
  }
  return undefined
}

async function readPersistedEvents(
  persistence: PersistenceLike,
  sessionId: string,
): Promise<SessionEventList> {
  if (typeof persistence.open === 'function') {
    const handle = await persistence.open(sessionId, 'read')
    try {
      return (await handle.read()).events
    } finally {
      await handle.close()
    }
  }
  if (typeof persistence.inspect === 'function') {
    return (await persistence.inspect(sessionId)).events
  }
  throw new Error('dsh session persistence does not expose a read API')
}

function messageTextOf(event: unknown): string {
  const data = (event as {
    data?: { message?: { content?: unknown }; content?: unknown }
  }).data
  const content = data?.message?.content ?? data?.content
  if (!Array.isArray(content)) return ''
  return content
    .filter(
      (block): block is { type: 'text'; text?: string } =>
        block !== null &&
        typeof block === 'object' &&
        (block as { type?: string }).type === 'text',
    )
    .map((block) => block.text ?? '')
    .join('')
    .trim()
}

function lastEventTimeMs(
  events: ReadonlyArray<{ time?: number } | undefined>,
): number | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event && Number.isFinite(event.time)) return event.time
  }
  return undefined
}

export function extractFields(session: SessionLike): SummaryFields {
  const events = sessionEvents(session)
  let objective = ''
  let resultText = ''
  let toolCount = 0
  let messageCount = 0
  for (const event of events) {
    if (!event) continue
    const type = (event as { type?: string }).type
    if (type === 'user/message') {
      messageCount += 1
      const text = messageTextOf(event)
      if (text && !objective) objective = text.slice(0, 200)
    } else if (type === 'assistant/message') {
      messageCount += 1
      const text = messageTextOf(event)
      if (text) resultText = text.slice(0, 1_000)
    } else if (type === 'tool/call') {
      toolCount += 1
    }
  }
  return {
    title: undefined,
    objective,
    resultText,
    toolCount,
    messageCount,
  }
}

export function messageCountOf(
  events: ReadonlyArray<{ type?: string; time?: number } | undefined>,
): number {
  let count = 0
  for (const event of events) {
    const type = event?.type
    if (type === 'user/message' || type === 'assistant/message') count += 1
  }
  return count
}

export function digestEventIdForRound(
  principalId: string,
  sessionId: string,
  roundCount: number,
): string {
  // Same round, same id — a restart or duplicate trigger cannot duplicate.
  return createHash('sha256')
    .update([principalId, 'consensus', 'digest', sessionId, String(roundCount)].join('\u0000'))
    .digest('hex')
}

export type DigestLeaseStatus = 'acquired' | 'busy' | 'unavailable'

export interface DigestLeaseResult {
  readonly status: DigestLeaseStatus
  readonly release?: () => void
}

/**
 * Use one lease file per round. A single shared file would incorrectly make
 * an unrelated session wait behind another session's summarization call.
 */
export function digestLeasePath(dshHome: string, eventId: string): string {
  const key = createHash('sha256').update(eventId).digest('hex')
  return resolve(dshHome, 'agent-society-digest-leases', `${key}.lease`)
}

function releaseDigestLease(lease: DigestLeaseResult | undefined): void {
  lease?.release?.()
}

/**
 * Acquire one best-effort cross-profile lease for a digest model call. A
 * filesystem failure returns `unavailable`, which deliberately lets the
 * caller continue with the existing event-id idempotency fallback.
 */
export function tryAcquireDigestLease(
  path: string,
  eventId: string,
  now = Date.now(),
): DigestLeaseResult {
  try {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
  } catch {
    return { status: 'unavailable' }
  }

  const owner = randomUUID()
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let descriptor: number | undefined
    try {
      descriptor = openSync(path, 'wx', 0o600)
      writeSync(
        descriptor,
        JSON.stringify({ owner, eventId, createdAt: now }) + '\n',
      )
      closeSync(descriptor)
      descriptor = undefined
      let released = false
      return {
        status: 'acquired',
        release: () => {
          if (released) return
          released = true
          try {
            const current = JSON.parse(readFileSync(path, 'utf8')) as {
              owner?: unknown
            }
            if (current.owner === owner) unlinkSync(path)
          } catch {
            // The lease may already have expired or been removed.
          }
        },
      }
    } catch (error) {
      if (descriptor !== undefined) {
        try {
          closeSync(descriptor)
        } catch {
          // Preserve the original acquisition failure.
        }
      }
      const code = (error as { code?: string }).code
      if (code !== 'EEXIST') return { status: 'unavailable' }
      try {
        const current = JSON.parse(readFileSync(path, 'utf8')) as {
          createdAt?: unknown
        }
        if (
          typeof current.createdAt === 'number' &&
          now - current.createdAt < DIGEST_LEASE_SECONDS * 1_000
        ) {
          return { status: 'busy' }
        }
        unlinkSync(path)
      } catch (readError) {
        if ((readError as { code?: string }).code === 'ENOENT') continue
        return { status: 'unavailable' }
      }
    }
  }
  return { status: 'unavailable' }
}

function loadState(path: string): DigestState {
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'))
    if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const record = parsed as Record<string, unknown>
      const result: DigestState = {}
      for (const [key, value] of Object.entries(record)) {
        if (
          value !== null &&
          typeof value === 'object' &&
          typeof (value as { count?: unknown }).count === 'number'
        ) {
          const lastPromptAt = (value as { lastPromptAt?: number }).lastPromptAt
          result[key] = {
            count: (value as { count: number }).count,
            ...(lastPromptAt === undefined ? {} : { lastPromptAt }),
            digestAt: (value as { digestAt?: number }).digestAt ?? 0,
          }
        }
      }
      return result
    }
  } catch {
    // Missing or partial state is a normal first run.
  }
  return {}
}

function saveState(path: string, state: DigestState): void {
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
