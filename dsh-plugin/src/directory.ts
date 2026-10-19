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

/** Prompt-facing directory data. Volatile activity timestamps and invocation
 * history deliberately stay out of this shape so an idle mirror refresh does
 * not invalidate the shared-context message. */
export interface SharedDirectoryRow {
  readonly session_id: string
  readonly actor_id?: string
  readonly title?: string
  readonly workspace: string
  readonly status: string
  readonly session_mode: string
  readonly tool_policy: string
}

/** Stable, semantic state used by the balanced shared-context renderer. */
export interface SharedContextState {
  readonly directorySeq: number
  readonly consensusSeq: number
  readonly consensusEntries: readonly ConsensusEntry[]
  readonly directoryRows: readonly SharedDirectoryRow[]
}

export type SharedContextUpdateKind = 'initial' | 'delta' | 'snapshot'

export interface SharedContextUpdate {
  readonly kind: SharedContextUpdateKind
  readonly text: string
  readonly fingerprint: string
}

export const BALANCED_INITIAL_BUDGET_CHARS = 1_600
export const BALANCED_DELTA_BUDGET_CHARS = 800
const BALANCED_CONSENSUS_LINE_CHARS = 220
const BALANCED_DIRECTORY_LINE_CHARS = 180

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

/** Projection-cache activity: title and lastPromptAt (ms epoch) per session. */
export function loadProjectionActivity(
  dshHome: string,
): Map<string, { title?: string; lastPromptAt?: number }> {
  const activity = new Map<string, { title?: string; lastPromptAt?: number }>()
  try {
    const cache = JSON.parse(
      readFileSync(join(dshHome, 'storages', 'session_projcache.json'), 'utf8'),
    ) as {
      tables?: {
        sessions?: Record<
          string,
          {
            rows?: {
              title?: { val?: unknown }
              sessionListMetadata?: { val?: { lastPromptAt?: unknown } }
            }
          }
        >
      }
    }
    const sessions = cache.tables?.sessions
    if (!sessions) return activity
    for (const [sessionId, record] of Object.entries(sessions)) {
      const rows = record?.rows
      const title = rows?.title?.val
      const lastPromptAt = rows?.sessionListMetadata?.val?.lastPromptAt
      const entry: { title?: string; lastPromptAt?: number } = {}
      if (typeof title === 'string' && title.length > 0) entry.title = title
      if (typeof lastPromptAt === 'number' && Number.isFinite(lastPromptAt)) {
        entry.lastPromptAt = lastPromptAt
      }
      if (entry.title !== undefined || entry.lastPromptAt !== undefined) {
        activity.set(sessionId, entry)
      }
    }
  } catch {
    // No projection cache is a normal cold start.
  }
  return activity
}

/**
 * Project a mirror into the semantic fields that may enter a model request.
 * The projection is intentionally independent of object insertion order and
 * `last_active_at`; both are cache-hostile sources of otherwise meaningless
 * prompt churn.
 */
export function normalizeSharedContextState(
  mirror: DirectoryMirror,
  currentSessionId?: string,
): SharedContextState {
  const latestBySession = new Map<string, ConsensusEntry>()
  const unscoped: ConsensusEntry[] = []
  for (const entry of mirror.consensus.entries) {
    if (
      entry === null ||
      typeof entry !== 'object' ||
      typeof entry.summary !== 'string' ||
      typeof entry.kind !== 'string'
    ) continue
    if (entry.session_id) {
      if (entry.session_id === currentSessionId) continue
      const previous = latestBySession.get(entry.session_id)
      if (previous === undefined || consensusEntryCompare(entry, previous) < 0) {
        latestBySession.set(entry.session_id, entry)
      }
    } else {
      unscoped.push(entry)
    }
  }
  const consensusEntries = [...latestBySession.values(), ...unscoped]
    .sort(consensusEntryCompare)
    .slice(0, MIRROR_MAX_CONSENSUS_ENTRIES)

  const directoryRows = Object.values(mirror.rows)
    .filter((row) => row.session_id !== currentSessionId)
    .map(toSharedDirectoryRow)
    .sort((a, b) => stringCompare(a.session_id, b.session_id))

  return {
    directorySeq: finiteRevision(mirror.seq),
    consensusSeq: finiteRevision(mirror.consensus.seq),
    consensusEntries,
    directoryRows,
  }
}

/** Stable semantic fingerprint; revisions are intentionally excluded. */
export function sharedContextStateFingerprint(state: SharedContextState): string {
  return JSON.stringify({
    consensusEntries: state.consensusEntries.map((entry) => ({
      seq: entry.seq,
      kind: entry.kind,
      ...(entry.session_id ? { session_id: entry.session_id } : {}),
      summary: entry.summary,
    })),
    directoryRows: state.directoryRows,
  })
}

/**
 * Render one cache-friendly update. A removed row/entry produces a fresh
 * compact snapshot because a delta cannot safely prove that no other stale
 * state remains in a model's earlier transcript.
 */
export function renderSharedContextUpdate(
  previous: SharedContextState | undefined,
  current: SharedContextState,
): SharedContextUpdate | undefined {
  const fingerprint = sharedContextStateFingerprint(current)
  if (previous === undefined) {
    if (!hasSharedContext(current)) return undefined
    return {
      kind: 'initial',
      text: renderCompactSnapshot(current, 'initial'),
      fingerprint,
    }
  }
  if (sharedContextStateFingerprint(previous) === fingerprint) return undefined

  const previousConsensus = new Map(
    previous.consensusEntries.map((entry) => [consensusEntryKey(entry), entry]),
  )
  const currentConsensus = new Map(
    current.consensusEntries.map((entry) => [consensusEntryKey(entry), entry]),
  )
  const previousDirectory = new Map(
    previous.directoryRows.map((row) => [row.session_id, row]),
  )
  const currentDirectory = new Map(
    current.directoryRows.map((row) => [row.session_id, row]),
  )

  const removed =
    [...previousConsensus.keys()].some((key) => !currentConsensus.has(key)) ||
    [...previousDirectory.keys()].some((key) => !currentDirectory.has(key))
  if (removed) {
    return {
      kind: 'snapshot',
      text: renderCompactSnapshot(current, 'snapshot'),
      fingerprint,
    }
  }

  const changes: string[] = []
  for (const entry of current.consensusEntries) {
    const old = previousConsensus.get(consensusEntryKey(entry))
    if (old === undefined) {
      changes.push(`+ 共识 ${renderConsensusEntry(entry)}`)
    } else if (!sameConsensusEntry(old, entry)) {
      changes.push(`~ 共识 ${renderConsensusEntry(entry)}`)
    }
  }
  for (const row of current.directoryRows) {
    const old = previousDirectory.get(row.session_id)
    if (old === undefined) {
      changes.push(`+ 目录 ${renderDirectoryRow(row)}`)
    } else if (!sameDirectoryRow(old, row)) {
      changes.push(`~ 目录 ${renderDirectoryRow(row)}`)
    }
  }
  if (changes.length === 0) return undefined

  const text = renderDelta(current, changes)
  if (text.length > BALANCED_DELTA_BUDGET_CHARS) {
    return {
      kind: 'snapshot',
      text: renderCompactSnapshot(current, 'snapshot'),
      fingerprint,
    }
  }
  return { kind: 'delta', text, fingerprint }
}

function finiteRevision(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function stringCompare(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

/** Oldest-to-newest comparison for latest-per-session selection, then newest
 * first for rendering. Returning a negative value means `a` is preferred. */
function consensusEntryCompare(a: ConsensusEntry, b: ConsensusEntry): number {
  if (a.seq !== b.seq) return b.seq - a.seq
  return (
    stringCompare(a.session_id ?? '', b.session_id ?? '') ||
    stringCompare(a.kind, b.kind) ||
    stringCompare(a.summary, b.summary)
  )
}

function consensusEntryKey(entry: ConsensusEntry): string {
  return entry.session_id
    ? `session:${entry.session_id}`
    : `event:${entry.seq}:${entry.kind}:${entry.summary}`
}

function sameConsensusEntry(a: ConsensusEntry, b: ConsensusEntry): boolean {
  return (
    a.seq === b.seq &&
    a.kind === b.kind &&
    a.session_id === b.session_id &&
    a.summary === b.summary
  )
}

function toSharedDirectoryRow(row: DirectoryRow): SharedDirectoryRow {
  const title = typeof row.title === 'string' ? row.title.trim() : ''
  return {
    session_id: row.session_id,
    ...(row.actor_id ? { actor_id: row.actor_id } : {}),
    ...(title ? { title } : {}),
    workspace: row.workspace || 'unknown workspace',
    status: row.status || 'unknown',
    session_mode: row.session_mode || 'unknown',
    tool_policy: row.tool_policy || 'unknown',
  }
}

function sameDirectoryRow(a: SharedDirectoryRow, b: SharedDirectoryRow): boolean {
  return (
    a.session_id === b.session_id &&
    a.actor_id === b.actor_id &&
    a.title === b.title &&
    a.workspace === b.workspace &&
    a.status === b.status &&
    a.session_mode === b.session_mode &&
    a.tool_policy === b.tool_policy
  )
}

function hasSharedContext(state: SharedContextState): boolean {
  return state.consensusEntries.length > 0 || state.directoryRows.length > 0
}

function clipLine(value: string, max: number): string {
  const compact = value.replace(/[\r\n]+/gu, ' ').trim()
  return compact.length <= max ? compact : `${compact.slice(0, max - 1)}…`
}

function renderConsensusEntry(entry: ConsensusEntry): string {
  const where = entry.session_id ? ` ${entry.session_id}` : ''
  return clipLine(`[${entry.kind}]${where}: ${entry.summary}`, BALANCED_CONSENSUS_LINE_CHARS)
}

function renderDirectoryRow(row: SharedDirectoryRow): string {
  const label = row.title || row.workspace || 'unknown workspace'
  return clipLine(
    `${row.session_id} | ${row.actor_id ?? '?'} | ${label} | ${row.status}`,
    BALANCED_DIRECTORY_LINE_CHARS,
  )
}

function renderDelta(state: SharedContextState, changes: readonly string[]): string {
  return [
    'AgentSociety 共享上下文增量（仅包含本轮变化）',
    `revision: directory=${state.directorySeq}; consensus=${state.consensusSeq}`,
    '未列出的条目保持不变；需要权威完整内容时使用 Hub MCP。',
    ...changes.map((change) => clipLine(change, BALANCED_DIRECTORY_LINE_CHARS)),
  ].join('\n')
}

function renderCompactSnapshot(
  state: SharedContextState,
  kind: 'initial' | 'snapshot',
): string {
  const consensus = state.consensusEntries.map(
    (entry) => `- ${renderConsensusEntry(entry)}`,
  )
  const directory = state.directoryRows.map(
    (row) => `- ${renderDirectoryRow(row)}`,
  )
  const header = [
    kind === 'initial'
      ? 'AgentSociety 共享上下文初始快照（有界提示）'
      : 'AgentSociety 共享上下文紧凑快照（状态删除或无法增量化）',
    `revision: directory=${state.directorySeq}; consensus=${state.consensusSeq}`,
    '完整 objective/result 和目录详情请使用 Hub MCP；当前 session 已排除。',
  ]
  const build = (left: readonly string[], right: readonly string[]): string => [
    ...header,
    ...(left.length > 0 ? ['共识（每 session 最新一条）：', ...left] : []),
    ...(right.length > 0 ? ['目录（按 session_id 排序）：', ...right] : []),
    ...(left.length === 0 && right.length === 0 ? ['当前没有可用共享条目。'] : []),
  ].join('\n')
  let text = build(consensus, directory)
  while (text.length > BALANCED_INITIAL_BUDGET_CHARS && directory.length > 0) {
    directory.pop()
    text = build(consensus, directory)
  }
  while (text.length > BALANCED_INITIAL_BUDGET_CHARS && consensus.length > 0) {
    consensus.pop()
    text = build(consensus, directory)
  }
  return text.slice(0, BALANCED_INITIAL_BUDGET_CHARS)
}

// ── Prompt injection (bounded) ────────────────────────────────────────────

export const DIRECTORY_INDEX_SECTION = 'agent-society:directory-index'
export const CONSENSUS_SECTION = 'agent-society:consensus'
export const PROMPT_BUDGET_CHARS = 4_000
const MAX_CONSENSUS_LINES = 8
const MAX_DIRECTORY_LINES = 20

/** One-line consensus summaries for the prompt (most recent first).
 *
 * KV-cache stability: this session's own digests are excluded. The digest
 * watcher writes a new entry whenever the session idles ~60s, so an active
 * conversation would otherwise change the injected bytes every round and
 * break the request prefix (cache miss) — while adding nothing the model
 * does not already have in the transcript.
 */
export function consensusPromptLines(
  mirror: DirectoryMirror,
  currentSessionId?: string,
): string[] {
  const lines: string[] = []
  const seen = new Set<string>()
  for (const entry of mirror.consensus.entries) {
    if (entry.session_id === currentSessionId) continue
    // The digest watcher writes one entry per ~60s idle gap, so an active
    // remote session can produce several entries with identical text. Keep
    // the newest copy of each (session, summary) pair so the bounded 8-line
    // budget is spent on distinct facts, not repeated "OK" digests.
    const key = `${entry.session_id ?? ''}|${entry.summary}`
    if (seen.has(key)) continue
    seen.add(key)
    const where = entry.session_id ? ` ${entry.session_id}` : ''
    lines.push(`- [${entry.kind}]${where}: ${entry.summary}`)
    if (lines.length >= MAX_CONSENSUS_LINES) break
  }
  return lines
}

/** Static workflow guide, registered as a system section so the model
 * always knows how to reach shared memory, even on turns where the
 * change-detected snapshot is not re-injected. */
export const WORKFLOW_GUIDE =
  '## AgentSociety 共享记忆\n' +
  '默认只追加一次紧凑初始快照，之后只追加结构化变化（最近一次见会话历史）；' +
  '需要当前或完整内容时用 hub_context_read / hub_directory_list 读取；' +
  '涉及某个会话时 hub_directory_get 下钻（depth 2 看其 digest）判断相关性后，' +
  'hub_ask 携带 target_session_id 在该会话上下文中发问（回答写回该会话）；' +
  '得出可复用结论时 hub_context_append 写回。'

/** Ranked directory index lines: working > recently active > recent rows. */
export function directoryPromptLines(
  mirror: DirectoryMirror,
  currentSessionId?: string,
): string[] {
  const rows = Object.values(mirror.rows).filter(
    (row) => row.session_id !== currentSessionId,
  )
  const rank = (row: DirectoryRow): number => (row.status === 'working' ? 0 : 1)
  // KV-cache stability: rank first, then session_id (stable byte order).
  // last_active_at changes on every mirror pull and would reorder rows
  // between assemblies, churning the prompt prefix for no semantic gain.
  // Status/title changes still update the text, but idle churn must not.
  rows.sort(
    (a, b) =>
      rank(a) - rank(b) ||
      (a.session_id < b.session_id ? -1 : a.session_id > b.session_id ? 1 : 0),
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
  currentSessionId?: string,
): Array<{ name: string; text: string }> {
  const consensusLines = consensusPromptLines(mirror, currentSessionId)
  const directoryLines = directoryPromptLines(mirror, currentSessionId)
  const build = (
    consensus: string[],
    directory: string[],
  ): Array<{ name: string; text: string }> => {
    const sections: Array<{ name: string; text: string }> = []
    if (consensus.length > 0) {
      sections.push({
        name: CONSENSUS_SECTION,
        text: '## 共享共识上下文（AgentSociety）\n' + consensus.join('\n'),
      })
    }
    if (directory.length > 0) {
      sections.push({
        name: DIRECTORY_INDEX_SECTION,
        text:
          '## 会话/Agent 目录（AgentSociety）\n' + directory.join('\n'),
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
