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
 * - `agent-society-*` task sessions are skipped: the worker task path
 *   already writes their digests.
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
interface SessionLike {
    readonly id: string;
    readonly events: ReadonlyArray<{
        time: number;
    } | undefined>;
}
export declare function apply(ctx: Context, config: Config): void;
export declare function extractFields(session: SessionLike): SummaryFields;
export declare function digestEventIdForRound(principalId: string, sessionId: string, roundCount: number): string;
export {};
