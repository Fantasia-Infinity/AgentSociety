/**
 * Deterministic consensus digest construction for AgentSociety workers.
 *
 * A digest is a bounded, structured summary of one task run written into the
 * Hub shared memory (scope 'consensus'). It is derived state: the dsh session
 * log and the Hub task/run records remain authoritative. The event_id is a
 * content-derived hash so a re-run (worker restart, retry) cannot duplicate
 * the entry.
 */

import { createHash } from 'node:crypto'

export const DIGEST_TTL_HOURS = 720 // 30 days
export const DIGEST_MAX_RESULT_CHARS = 1_000

export interface DigestInput {
  readonly principalId: string
  readonly sessionId: string
  readonly actorId: string
  readonly nodeId: string
  readonly taskId?: string
  readonly runId?: string
  readonly title: string | undefined
  readonly workspace: string
  readonly objective: string
  readonly status: string
  readonly resultText: string
  readonly toolCount: number
  readonly messageCount: number
  readonly createdAt: number
}

export interface ConsensusDigest {
  readonly scope: 'consensus'
  readonly kind: 'digest'
  readonly event_id: string
  readonly principal_id: string
  readonly session_id: string
  readonly actor_id: string
  readonly node_id: string
  readonly ttl_hours: number
  readonly payload: {
    readonly session_id: string
    readonly title: string | undefined
    readonly workspace: string
    readonly objective: string
    readonly task_id: string | undefined
    readonly run_id: string | undefined
    readonly status: string
    readonly result: string
    readonly toolCount: number
    readonly messageCount: number
    readonly createdAt: number
    /** LLM or deterministic summary for interactive-session digests. */
    readonly summary?: string
  }
}

/** Stable idempotency key: one digest per (principal, session, task run). */
export function digestEventId(input: DigestInput): string {
  return createHash('sha256')
    .update(
      [
        input.principalId,
        'consensus',
        'digest',
        input.sessionId,
        input.taskId ?? '',
        input.runId ?? '',
      ].join('\u0000'),
    )
    .digest('hex')
    .slice(0, 64)
}

/**
 * Build one consensus digest. `eventId` overrides the derived idempotency
 * key (used by interactive-session digests, whose round counter is the
 * deduplication salt); `summary` is the optional LLM/derived summary text
 * attached to the payload.
 */
export function buildSessionDigest(
  input: DigestInput,
  options: { eventId?: string; summary?: string } = {},
): ConsensusDigest {
  const result = input.resultText.trim().slice(0, DIGEST_MAX_RESULT_CHARS)
  return {
    scope: 'consensus',
    kind: 'digest',
    event_id: options.eventId ?? digestEventId(input),
    principal_id: input.principalId,
    session_id: input.sessionId,
    actor_id: input.actorId,
    node_id: input.nodeId,
    ttl_hours: DIGEST_TTL_HOURS,
    payload: {
      session_id: input.sessionId,
      title: input.title,
      workspace: input.workspace,
      objective: input.objective.trim().slice(0, 2_000),
      task_id: input.taskId,
      run_id: input.runId,
      status: input.status,
      result,
      toolCount: input.toolCount,
      messageCount: input.messageCount,
      createdAt: input.createdAt,
      ...(options.summary ? { summary: options.summary } : {}),
    },
  }
}
