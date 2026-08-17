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
export const inject: string[] = []

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
  }

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
    const rows = await hub.listDirectory({
      after_seq: state.mirror.seq,
      limit: 200,
    })
    if (rows.length === 0) return
    let maxSeq = state.mirror.seq
    for (const event of rows) {
      const seq = Number(event.seq)
      const sessionId = event.session_id
      if (typeof sessionId !== 'string' || !sessionId) continue
      const payload = event.payload as DirectoryRow | undefined
      if (!payload || typeof payload !== 'object') continue
      state.mirror.rows[sessionId] = {
        session_id: sessionId,
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
    saveMirror(path, state.mirror)
    ctx.logger.debug(
      `agent-society-directory pulled ${rows.length} row(s) (seq ${state.mirror.seq})`,
    )
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
    // Push at most once per pull window per session to keep the log bounded.
    for (const header of headers) {
      if (!header || typeof header.id !== 'string') continue
      const row = buildLocalRow({
        sessionId: header.id,
        title: titles.get(header.id),
        workspace: header.cwd ?? workspaceRoot,
        lastActiveAt: now,
        sessionMode: config.sessionMode ?? 'per_task',
        toolPolicy: config.toolPolicy ?? 'full',
      })
      await hub.upsertDirectoryRow({
        session_id: header.id,
        row,
        principal_id: principalId,
        actor_id: actorId,
        node_id: nodeId,
      })
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
