/**
 * Prompt/cache diagnostics that never persist prompt text, summaries, or
 * credentials. The file is deliberately best-effort: an unwritable metrics
 * path must not affect an interactive request or a digest.
 */

import { createHash } from 'node:crypto'
import {
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from 'node:fs'
import { dirname, join } from 'node:path'

export type ContextMetricKind = 'initial' | 'delta' | 'snapshot'

export interface AgentSocietyMetrics {
  readonly version: 1
  readonly updatedAt: number
  readonly cacheReadTokens: number
  readonly inputTokens: number
  readonly uncachedInputTokens: number
  readonly contextInitialBytes: number
  readonly contextDeltaBytes: number
  readonly contextSnapshotBytes: number
  readonly contextUpdatesPerSession: Readonly<Record<string, {
    readonly initial: number
    readonly delta: number
    readonly snapshot: number
    readonly lastBytes: number
  }>>
  readonly digestModelCalls: number
  readonly digestDeduplicatedCalls: number
  readonly hubToolSchemaHash?: string
}

export function contextMetricsPath(dshHome: string): string {
  return join(dshHome, 'agent-society-context-metrics.json')
}

export function recordAgentSocietyMetrics(
  dshHome: string,
  update: {
    readonly context?: {
      readonly kind: ContextMetricKind
      readonly bytes: number
      readonly sessionId?: string
    }
    readonly digestModelCall?: boolean
    readonly digestDeduplicatedCall?: boolean
    readonly hubToolSchemaHash?: string
    readonly usage?: {
      readonly inputTokens: number
      readonly cacheReadTokens?: number
      readonly cacheWriteTokens?: number
    }
  },
): void {
  try {
    const path = contextMetricsPath(dshHome)
    const current = loadMetrics(path)
    const sessions: Record<string, {
      initial: number
      delta: number
      snapshot: number
      lastBytes: number
    }> = { ...current.contextUpdatesPerSession }
    const context = update.context
    if (context?.sessionId) {
      const previous = sessions[context.sessionId] ?? {
        initial: 0,
        delta: 0,
        snapshot: 0,
        lastBytes: 0,
      }
      sessions[context.sessionId] = {
        ...previous,
        [context.kind]: previous[context.kind] + 1,
        lastBytes: Math.max(0, Math.floor(context.bytes)),
      }
      const keys = Object.keys(sessions)
      if (keys.length > 256) delete sessions[keys[0] as string]
    }
    const next: AgentSocietyMetrics = {
      version: 1,
      updatedAt: Date.now(),
      cacheReadTokens:
        current.cacheReadTokens + Math.max(0, Math.floor(update.usage?.cacheReadTokens ?? 0)),
      inputTokens:
        current.inputTokens +
        Math.max(
          0,
          Math.floor(
            (update.usage?.inputTokens ?? 0) +
            (update.usage?.cacheReadTokens ?? 0) +
            (update.usage?.cacheWriteTokens ?? 0),
          ),
        ),
      uncachedInputTokens:
        current.uncachedInputTokens +
        Math.max(0, Math.floor(update.usage?.inputTokens ?? 0)),
      contextInitialBytes:
        current.contextInitialBytes +
        (context?.kind === 'initial' ? Math.max(0, Math.floor(context.bytes)) : 0),
      contextDeltaBytes:
        current.contextDeltaBytes +
        (context?.kind === 'delta' ? Math.max(0, Math.floor(context.bytes)) : 0),
      contextSnapshotBytes:
        current.contextSnapshotBytes +
        (context?.kind === 'snapshot' ? Math.max(0, Math.floor(context.bytes)) : 0),
      contextUpdatesPerSession: sessions,
      digestModelCalls:
        current.digestModelCalls + (update.digestModelCall === true ? 1 : 0),
      digestDeduplicatedCalls:
        current.digestDeduplicatedCalls +
        (update.digestDeduplicatedCall === true ? 1 : 0),
      ...(update.hubToolSchemaHash
        ? { hubToolSchemaHash: update.hubToolSchemaHash }
        : current.hubToolSchemaHash
          ? { hubToolSchemaHash: current.hubToolSchemaHash }
          : {}),
    }
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
    const temporary = `${path}.${process.pid}.tmp`
    writeFileSync(temporary, `${JSON.stringify(next, null, 2)}\n`, {
      encoding: 'utf8',
      mode: 0o600,
    })
    renameSync(temporary, path)
  } catch {
    // Diagnostics are strictly non-functional.
  }
}

export function hashToolSchemas(tools: readonly unknown[]): string {
  return createHash('sha256')
    .update(JSON.stringify(stableValue(tools)))
    .digest('hex')
}

function loadMetrics(path: string): AgentSocietyMetrics {
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf8')) as Partial<AgentSocietyMetrics>
    if (
      parsed.version === 1 &&
      typeof parsed.contextInitialBytes === 'number' &&
      typeof parsed.contextDeltaBytes === 'number' &&
      typeof parsed.contextSnapshotBytes === 'number' &&
      typeof parsed.digestModelCalls === 'number' &&
      typeof parsed.digestDeduplicatedCalls === 'number'
    ) {
      return {
      version: 1,
      updatedAt: typeof parsed.updatedAt === 'number' ? parsed.updatedAt : 0,
        cacheReadTokens: parsed.cacheReadTokens ?? 0,
        inputTokens: parsed.inputTokens ?? 0,
        uncachedInputTokens: parsed.uncachedInputTokens ?? 0,
        contextInitialBytes: parsed.contextInitialBytes,
        contextDeltaBytes: parsed.contextDeltaBytes,
        contextSnapshotBytes: parsed.contextSnapshotBytes,
        contextUpdatesPerSession: parsed.contextUpdatesPerSession ?? {},
        digestModelCalls: parsed.digestModelCalls,
        digestDeduplicatedCalls: parsed.digestDeduplicatedCalls,
        ...(parsed.hubToolSchemaHash
          ? { hubToolSchemaHash: parsed.hubToolSchemaHash }
          : {}),
      }
    }
  } catch {
    // Missing or partial metrics are normal on first start.
  }
  return {
    version: 1,
    updatedAt: 0,
    cacheReadTokens: 0,
    inputTokens: 0,
    uncachedInputTokens: 0,
    contextInitialBytes: 0,
    contextDeltaBytes: 0,
    contextSnapshotBytes: 0,
    contextUpdatesPerSession: {},
    digestModelCalls: 0,
    digestDeduplicatedCalls: 0,
  }
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([key, child]) => [key, stableValue(child)]),
    )
  }
  return value
}
