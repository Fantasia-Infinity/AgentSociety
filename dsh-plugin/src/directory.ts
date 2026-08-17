/**
 * Session directory helpers shared by the worker (invocation upserts) and
 * the `agent-society-directory` plugin (local-session sync + mirror cache).
 *
 * The mirror (`~/.dsh/agent-society-directory.json`, 0600) is a local cache
 * of the Hub's per-principal directory: own sessions keep full depth-0/1
 * rows, other sessions keep their latest pushed row. It is derived state —
 * the Hub log remains authoritative.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

export interface DirectoryRow {
  readonly session_id: string
  readonly actor_id?: string
  readonly title?: string
  readonly workspace: string
  readonly status: string
  readonly last_active_at: number
  readonly session_mode: string
  readonly tool_policy: string
  readonly invocations: ReadonlyArray<{
    readonly task_id?: string
    readonly run_id?: string
    readonly objective: string
    readonly status: string
    readonly at: number
  }>
}

/** One consensus entry cached for prompt injection (bounded). */
export interface ConsensusEntry {
  readonly seq: number
  readonly kind: string
  readonly session_id?: string
  readonly summary: string
}

export interface DirectoryMirror {
  seq: number
  updated_at: number
  rows: Record<string, DirectoryRow>
  consensus: {
    seq: number
    entries: ConsensusEntry[]
  }
}

export const MIRROR_MAX_INVOCATIONS = 10
export const MIRROR_MAX_CONSENSUS_ENTRIES = 24

export function mirrorPath(dshHome: string): string {
  return join(dshHome, 'agent-society-directory.json')
}

export function loadMirror(path: string): DirectoryMirror {
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'))
    if (
      parsed !== null &&
      typeof parsed === 'object' &&
      !Array.isArray(parsed) &&
      typeof (parsed as Record<string, unknown>).rows === 'object'
    ) {
      const record = parsed as {
        seq?: unknown
        updated_at?: unknown
        rows?: unknown
        consensus?: unknown
      }
      const rows = record.rows as Record<string, DirectoryRow>
      const normalized: Record<string, DirectoryRow> = {}
      for (const [sessionId, row] of Object.entries(rows)) {
        if (row !== null && typeof row === 'object' && typeof row.session_id === 'string') {
          normalized[sessionId] = row
        }
      }
      let consensus = { seq: 0, entries: [] as ConsensusEntry[] }
      const rawConsensus = record.consensus as
        | { seq?: unknown; entries?: unknown }
        | undefined
      if (
        rawConsensus !== null &&
        typeof rawConsensus === 'object' &&
        Array.isArray(rawConsensus.entries)
      ) {
        consensus = {
          seq: typeof rawConsensus.seq === 'number' ? rawConsensus.seq : 0,
          entries: rawConsensus.entries.filter(
            (entry): entry is ConsensusEntry =>
              entry !== null &&
              typeof entry === 'object' &&
              typeof (entry as ConsensusEntry).kind === 'string' &&
              typeof (entry as ConsensusEntry).summary === 'string',
          ),
        }
      }
      return {
        seq: typeof record.seq === 'number' ? record.seq : 0,
        updated_at: typeof record.updated_at === 'number' ? record.updated_at : 0,
        rows: normalized,
        consensus,
      }
    }
  } catch {
    // Missing or partial mirror is a normal first run.
  }
  return { seq: 0, updated_at: 0, rows: {}, consensus: { seq: 0, entries: [] } }
}

export function saveMirror(path: string, mirror: DirectoryMirror): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
  writeFileSync(path, `${JSON.stringify(mirror, null, 2)}\n`, {
    encoding: 'utf8',
    mode: 0o600,
  })
}

/** Merge one invocation into the row for a session (bounded history). */
export function mergeInvocation(options: {
  row: DirectoryRow | undefined
  sessionId: string
  workspace: string
  title: string | undefined
  sessionMode: string
  toolPolicy: string
  actorId?: string
  invocation: DirectoryRow['invocations'][number]
}): DirectoryRow {
  const base: DirectoryRow = options.row ?? {
    session_id: options.sessionId,
    workspace: options.workspace,
    status: 'idle',
    last_active_at: options.invocation.at,
    session_mode: options.sessionMode,
    tool_policy: options.toolPolicy,
    invocations: [],
  }
  const invocations = [options.invocation, ...base.invocations]
    .filter(
      (item, index, all) =>
        all.findIndex((other) => other.run_id === item.run_id) === index,
    )
    .slice(0, MIRROR_MAX_INVOCATIONS)
  return {
    ...base,
    session_id: options.sessionId,
    ...(options.actorId ? { actor_id: options.actorId } : {}),
    ...(options.title ? { title: options.title } : {}),
    workspace: options.workspace,
    status: options.invocation.status === 'completed' ? 'done' : 'failed',
    last_active_at: options.invocation.at,
    invocations,
  }
}

/** Build a depth-0/1 row for a local session from its persistence header. */
export function buildLocalRow(options: {
  sessionId: string
  actorId: string
  title: string | undefined
  workspace: string
  lastActiveAt: number
  sessionMode: string
  toolPolicy: string
}): DirectoryRow {
  return {
    session_id: options.sessionId,
    actor_id: options.actorId,
    ...(options.title ? { title: options.title } : {}),
    workspace: options.workspace,
    status: 'idle',
    last_active_at: options.lastActiveAt,
    session_mode: options.sessionMode,
    tool_policy: options.toolPolicy,
    invocations: [],
  }
}

/** Read session titles from the dsh projection cache when present. */
export function loadProjectionTitles(dshHome: string): Map<string, string> {
  const titles = new Map<string, string>()
  try {
    const cache = JSON.parse(
      readFileSync(join(dshHome, 'storages', 'session_projcache.json'), 'utf8'),
    ) as {
      tables?: { sessions?: Record<string, { rows?: { title?: { val?: unknown } } }> }
    }
    const sessions = cache.tables?.sessions
    if (!sessions) return titles
    for (const [sessionId, record] of Object.entries(sessions)) {
      const title = record?.rows?.title?.val
      if (typeof title === 'string' && title.length > 0) {
        titles.set(sessionId, title)
      }
    }
  } catch {
    // No projection cache is a normal cold start.
  }
  return titles
}

// ── Prompt injection (bounded) ────────────────────────────────────────────

export const DIRECTORY_INDEX_SECTION = 'agent-society:directory-index'
export const CONSENSUS_SECTION = 'agent-society:consensus'
export const PROMPT_BUDGET_CHARS = 4_000
const MAX_CONSENSUS_LINES = 8
const MAX_DIRECTORY_LINES = 20

/** One-line consensus summaries for the prompt (most recent first). */
export function consensusPromptLines(mirror: DirectoryMirror): string[] {
  return mirror.consensus.entries.slice(0, MAX_CONSENSUS_LINES).map((entry) => {
    const where = entry.session_id ? ` ${entry.session_id}` : ''
    return `- [${entry.kind}]${where}: ${entry.summary}`
  })
}

/** Ranked directory index lines: working > recently active > recent rows. */
export function directoryPromptLines(mirror: DirectoryMirror): string[] {
  const rows = Object.values(mirror.rows)
  const rank = (row: DirectoryRow): number => (row.status === 'working' ? 0 : 1)
  rows.sort(
    (a, b) => rank(a) - rank(b) || b.last_active_at - a.last_active_at,
  )
  return rows.slice(0, MAX_DIRECTORY_LINES).map((row) => {
    const label = row.title && row.title.trim()
      ? row.title.trim()
      : row.workspace || 'unknown workspace'
    return `- ${row.session_id} | ${row.actor_id ?? '?'} | ${label} | ${row.status}`
  })
}

/**
 * Build the two bounded prompt sections for one assembly. Total text stays
 * under {@link PROMPT_BUDGET_CHARS}; entries are dropped from the directory
 * index first, then the consensus list, until the budget fits.
 */
export function buildSharedContextSections(
  mirror: DirectoryMirror,
): Array<{ name: string; text: string }> {
  const consensusLines = consensusPromptLines(mirror)
  const directoryLines = directoryPromptLines(mirror)
  const build = (
    consensus: string[],
    directory: string[],
  ): Array<{ name: string; text: string }> => {
    const sections: Array<{ name: string; text: string }> = []
    if (consensus.length > 0) {
      sections.push({
        name: CONSENSUS_SECTION,
        text:
          '## 共享共识上下文（AgentSociety）\n' +
          consensus.join('\n') +
          '\n（共享记忆：用 hub_context_read 读取详情）',
      })
    }
    if (directory.length > 0) {
      sections.push({
        name: DIRECTORY_INDEX_SECTION,
        text:
          '## 会话/Agent 目录（AgentSociety）\n' +
          directory.join('\n') +
          '\n（可跨设备协作：用 hub_directory_get / hub_ask 查询详情与交互）',
      })
    }
    return sections
  }
  let sections = build(consensusLines, directoryLines)
  let total = sections.reduce((sum, section) => sum + section.text.length, 0)
  while (total > PROMPT_BUDGET_CHARS && directoryLines.length > 0) {
    directoryLines.pop()
    sections = build(consensusLines, directoryLines)
    total = sections.reduce((sum, section) => sum + section.text.length, 0)
  }
  while (total > PROMPT_BUDGET_CHARS && consensusLines.length > 0) {
    consensusLines.pop()
    sections = build(consensusLines, directoryLines)
    total = sections.reduce((sum, section) => sum + section.text.length, 0)
  }
  return sections
}
