/**
 * LLM summarization for interactive-session consensus digests.
 *
 * KV-cache rules (non-negotiable):
 * - The live-session mode reuses the target session's OWN derived messages
 *   and its last persisted request/header system prompt, tools, route, and
 *   adapter config. It appends ONLY the summarization instruction as a
 *   trailing user message, so the session's prefix is not borrowed from a
 *   different session or process-global assembly.
 * - The standalone mode never copies session context: a fixed system prompt
 *   plus one structured user payload. It is the fallback when no live session
 *   header is available.
 * - Both modes are bounded: default maxTokens 768, hard cap 1024, output clipped to
 *   {@link MAX_SUMMARY_CHARS}; failures fall back to the deterministic
 *   extractor so a consensus digest is still produced.
 */
import type { Context } from '@deepseek-ai/cordis';
import { type Message } from '@deepseek-ai/dsh-llm';
import type { EpochHeader, Session } from '@deepseek-ai/dsh-session';
export declare const MAX_SUMMARY_CHARS = 600;
export declare const DEFAULT_SUMMARY_MAX_TOKENS = 768;
export declare const SUMMARY_MAX_TOKENS_HARD_LIMIT = 1024;
export declare const SUMMARY_TIMEOUT_MS = 30000;
export declare const SUMMARIZE_PROMPT: string;
export declare const SUMMARIZE_SYSTEM: string;
export interface SummaryFields {
    readonly title: string | undefined;
    readonly objective: string;
    readonly resultText: string;
    readonly toolCount: number;
    readonly messageCount: number;
}
/** Deterministic fallback: bounded field concatenation, no LLM involved. */
export declare function deterministicSummary(fields: SummaryFields): string;
/** Live-session mode: target session header/history prefix + trailing instruction. */
export declare function summarizeLiveSession(options: {
    ctx: Context;
    session: Session;
    requestHeader: EpochHeader;
    maxTokens: number;
}): Promise<string>;
/** Standalone mode: fixed system prefix plus a structured dynamic user payload. */
export declare function summarizeStandalone(options: {
    ctx: Context;
    fields: SummaryFields;
    provider: string;
    model: string;
    maxTokens: number;
}): Promise<string>;
export type { Message };
