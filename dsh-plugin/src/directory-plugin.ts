/**
 * AgentSociety directory sync plugin.
 *
 * Runs inside every hub-connected dsh process (TUI / Web / worker). It
 * (a) pushes depth-0/1 rows for the local sessions visible through dsh
 * session persistence, and (b) maintains the local mirror of the Hub's
 * per-principal directory (`~/.dsh/agent-society-directory.json`) via
 * incremental pulls. Staleness is bounded by `pullSeconds` (default 10s);
 * the worker plugin additionally pushes invocation rows on task run end.
 */

import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/cordis-plugin-timer'
import Schema from '@deepseek-ai/schemastery'
import { homedir, hostname, userInfo } from 'node:os'
import { resolve } from 'node:path'

import { HubClient } from './hub-client.js'
import {
  buildLocalRow,
  loadMirror,
  loadProjectionTitles,
  mirrorPath,
  saveMirror,
  type DirectoryRow,
} from './directory.js'

export const name = 'agent-society-directory'
export const inject = ['timer']

export interface Config {
  hubUrl?: string
  hubTokenEnv?: string
  principalId?: string
  actorId?: string
  nodeId?: string
  workspaceRoot?: string
  sessionMode?: string
  toolPolicy?: string
  pullSeconds?: number
}

export const Config: Schema<Config> = Schema.object({
  hubUrl: Schema.string().required(false),
  hubTokenEnv: Schema.string().required(false),
  principalId: Schema.string().required(false),
  actorId: Schema.string().required(false),
  nodeId: Schema.string().required(false),
  workspaceRoot: Schema.string().required(false),
  sessionMode: Schema.string().required(false),
  toolPolicy: Schema.string().required(false),
  pullSeconds: Schema.number().min(2).default(10),
})

interface SessionHeaderLike {
  readonly id: string
  readonly createdAt: number
  readonly cwd?: string
}

interface PersistenceLike {
  list(): Promise<readonly SessionHeaderLike[]>
}

export function apply(ctx: Context, config: Config): void {
  const hubUrl = config.hubUrl ?? process.env.AGENT_SOCIETY_HUB_URL?.trim()
  const tokenEnv = config.hubTokenEnv ?? 'AGENT_SOCIETY_HUB_TOKEN'
  const hubToken = process.env[tokenEnv]?.trim()
  if (!hubUrl || !hubToken) {
    ctx.logger.warn(
      'agent-society-directory: AGENT_SOCIETY_HUB_URL and AGENT_SOCIETY_HUB_TOKEN are required; directory sync stays idle',
    )
    return
  }

  const owner = stableSlug(userInfo().username)
  const host = stableSlug(hostname())
  const principalId = config.principalId ?? `human-${owner}`
  const actorId = config.actorId ?? `agent-society-${host}`
  const nodeId = config.nodeId ?? host
  const workspaceRoot = resolve(config.workspaceRoot ?? process.cwd())
  const pullSeconds = config.pullSeconds ?? 10
  const hub = new HubClient(hubUrl, hubToken)
  const path = mirrorPath(process.env.DSH_HOME?.trim() || resolve(homedir(), '.dsh'))
  const state = {
    mirror: loadMirror(path),
    lastLocalPush: 0,
    /** session_id -> JSON fingerprint of the last pushed local row. */
    pushedRows: {} as Record<string, string>,
    /** Tick counter driving the periodic full mirror rebuild. */
    syncTicks: 0,
  }
  // Rebuild the mirror from the hub's deduplicated directory every N syncs
  // (10s * 30 = 5min). Incremental pulls never observe deletions, so rows
  // whose sessions were pruned (cleanup, TTL) would stay in the mirror and
  // keep showing up in prompt injection and directory views forever.
  const FULL_PULL_EVERY = 30

  const timer = ctx.setInterval(() => {
    void sync()
  }, pullSeconds * 1_000)
  ctx.effect(() => () => timer())
  void sync()

  async function sync(): Promise<void> {
    try {
      await pullDirectory()
      await pushLocalSessions()
    } catch (error) {
      ctx.logger.warn(
        `agent-society-directory sync failed: ${error instanceof Error ? error.message : String(error)}`,
      )
    }
  }

  async function pullDirectory(): Promise<void> {
    const full = state.syncTicks % FULL_PULL_EVERY === 0
    state.syncTicks += 1
    const rows = await hub.listDirectory({
      // Full pulls start from seq 0: list_directory deduplicates to the
      // latest row per session, so rebuilding from the hub's full set drops
      // mirror rows whose sessions no longer exist on the hub.
      after_seq: full ? 0 : state.mirror.seq,
      limit: full ? 500 : 200,
    })
    if (full) state.mirror.rows = {}
    let maxSeq = full ? 0 : state.mirror.seq
    for (const event of rows) {
      const seq = Number(event.seq)
      const sessionId = event.session_id
      if (typeof sessionId !== 'string' || !sessionId) continue
      const payload = event.payload as DirectoryRow | undefined
      if (!payload || typeof payload !== 'object') continue
      state.mirror.rows[sessionId] = {
        session_id: sessionId,
        ...(typeof event.actor_id === 'string' ? { actor_id: event.actor_id } : {}),
        workspace: String(payload.workspace ?? ''),
        status: String(payload.status ?? 'idle'),
        last_active_at: Number(payload.last_active_at ?? 0),
        session_mode: String(payload.session_mode ?? 'per_task'),
        tool_policy: String(payload.tool_policy ?? 'full'),
        ...(typeof payload.title === 'string' ? { title: payload.title } : {}),
        invocations: Array.isArray(payload.invocations) ? payload.invocations : [],
      }
      if (seq > maxSeq) maxSeq = seq
    }
    state.mirror = { ...state.mirror, seq: maxSeq, updated_at: Date.now() }
    await pullConsensus(full)
    saveMirror(path, state.mirror)
    ctx.logger.debug(
      `agent-society-directory pulled ${rows.length} row(s) (seq ${state.mirror.seq})`,
    )
  }

  /** Incremental pull of consensus entries into the mirror cache. */
  async function pullConsensus(full: boolean): Promise<void> {
    const events = await hub.listSharedEvents({
      after_seq: full ? 0 : state.mirror.consensus.seq,
      scope: 'consensus',
      limit: full ? 500 : 200,
    })
    if (events.length === 0) return
    let maxSeq = full ? 0 : state.mirror.consensus.seq
    // Full pulls rebuild the entries from the hub's live set, dropping
    // expired/deleted entries that incremental pulls never observe.
    const entries = full ? [] : [...state.mirror.consensus.entries]
    for (const event of events) {
      const seq = Number(event.seq)
      if (!Number.isFinite(seq) || seq <= maxSeq) continue
      const kind = String(event.kind ?? 'note')
      const payload = event.payload as { summary?: unknown; result?: unknown; title?: unknown } | undefined
      const summary = summarizeConsensus(kind, payload)
      if (!summary) continue
      entries.unshift({
        seq,
        kind,
        ...(typeof event.session_id === 'string' && event.session_id
          ? { session_id: event.session_id }
          : {}),
        summary,
      })
      if (seq > maxSeq) maxSeq = seq
    }
    state.mirror = {
      ...state.mirror,
      consensus: { seq: maxSeq, entries: entries.slice(0, 24) },
    }
  }

  async function pushLocalSessions(): Promise<void> {
    const persistence = ctx.get('sessionPersistence') as PersistenceLike | undefined
    if (!persistence || typeof persistence.list !== 'function') return
    const headers = await persistence.list()
    if (headers.length === 0) return
    const titles = loadProjectionTitles(
      process.env.DSH_HOME?.trim() || resolve(homedir(), '.dsh'),
    )
    const now = Date.now()
    // Push only rows whose content changed since the last sync: the Hub
    // directory is deduplicated by latest-seq, and re-pushing unchanged
    // rows on every 10s cycle would grow the shared event log without bound.
    for (const header of headers) {
      if (!header || typeof header.id !== 'string') continue
      const row = buildLocalRow({
        sessionId: header.id,
        actorId,
        title: titles.get(header.id),
        workspace: header.cwd ?? workspaceRoot,
        lastActiveAt: now,
        sessionMode: config.sessionMode ?? 'per_task',
        toolPolicy: config.toolPolicy ?? 'full',
      })
      const fingerprint = JSON.stringify({
        ...row,
        // last_active_at is a display timestamp that advances every cycle;
        // it must not make the row look "changed" when nothing else did.
        last_active_at: undefined,
      })
      if (state.pushedRows[header.id] === fingerprint) continue
      await hub.upsertDirectoryRow({
        session_id: header.id,
        row,
        principal_id: principalId,
        actor_id: actorId,
        node_id: nodeId,
      })
      state.pushedRows[header.id] = fingerprint
      state.mirror.rows[header.id] = row
    }
    state.lastLocalPush = now
    saveMirror(path, state.mirror)
  }
}

function stableSlug(value: string): string {
  const slug = value.toLowerCase().replace(/[^a-z0-9._-]+/gu, '-')
  return slug.replace(/^-+|-+$/gu, '') || 'node'
}

/** Deterministic one-line summary for a consensus entry. */
function summarizeConsensus(
  kind: string,
  payload: { summary?: unknown; result?: unknown; title?: unknown } | undefined,
): string | undefined {
  if (!payload || typeof payload !== 'object') return undefined
  const candidates: unknown[] = [
    payload.summary,
    payload.title,
    payload.result,
  ]
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) {
      const line = candidate.replace(/\s+/gu, ' ').trim()
      return line.length > 160 ? `${line.slice(0, 160)}…` : line
    }
  }
  return undefined
}
