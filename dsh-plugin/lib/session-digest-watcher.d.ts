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
import type { Context } from '@deepseek-ai/cordis';
import Schema from '@deepseek-ai/schemastery';
import { type SummaryFields } from './summarizer.js';
export declare const name = "agent-society-session-digest";
export declare const inject: string[];
export interface Config {
    hubUrl?: string;
    hubTokenEnv?: string;
    principalId?: string;
    actorId?: string;
    nodeId?: string;
    workspaceRoot?: string;
    provider?: string;
    model?: string;
    maxTokens?: number;
    pollSeconds?: number;
    idleSeconds?: number;
    /** Summarize with the LLM (default on); 0 falls back to deterministic. */
    summarize?: boolean;
    /** Allow digest writes at all (AGENT_SOCIETY_CONTEXT). */
    contextEnabled?: boolean;
}
export declare const Config: Schema<Config>;
interface SessionEventLike {
    readonly type?: string;
    readonly time?: number;
    readonly data?: unknown;
}
type SessionEventList = ReadonlyArray<SessionEventLike | undefined>;
interface SessionLike {
    readonly id: string;
    readonly snapshotEvents?: () => SessionEventList;
    /** Compatibility face for older dsh releases and test doubles. */
    readonly events?: SessionEventList;
}
export declare const DIGEST_LEASE_SECONDS = 90;
export declare function apply(ctx: Context, config: Config): void;
export declare function extractFields(session: SessionLike): SummaryFields;
export declare function messageCountOf(events: ReadonlyArray<{
    type?: string;
    time?: number;
} | undefined>): number;
export declare function digestEventIdForRound(principalId: string, sessionId: string, roundCount: number): string;
export type DigestLeaseStatus = 'acquired' | 'busy' | 'unavailable';
export interface DigestLeaseResult {
    readonly status: DigestLeaseStatus;
    readonly release?: () => void;
}
/**
 * Use one lease file per round. A single shared file would incorrectly make
 * an unrelated session wait behind another session's summarization call.
 */
export declare function digestLeasePath(dshHome: string, eventId: string): string;
/**
 * Acquire one best-effort cross-profile lease for a digest model call. A
 * filesystem failure returns `unavailable`, which deliberately lets the
 * caller continue with the existing event-id idempotency fallback.
 */
export declare function tryAcquireDigestLease(path: string, eventId: string, now?: number): DigestLeaseResult;
export {};
