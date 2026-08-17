/**
 * LLM summarization for interactive-session consensus digests.
 *
 * KV-cache rules (non-negotiable):
 * - The live-session mode reuses the session's OWN derived messages
 *   (`session.deriveMessages()`, byte-identical to what dsh just sent the
 *   model) plus the cached assembly system/tools, and appends ONLY the
 *   summarization instruction as a trailing user message. The prefix
 *   (system + tools + full history) is unchanged, so provider prefix
 *   caches hit on the hot session prefix.
 * - The standalone mode never copies session context: a fixed template
 *   prefix plus extracted fields. It is the fallback when no live session
 *   (or no assembly cache) is available.
 * - Both modes are bounded: maxTokens 2048, output clipped to
 *   {@link MAX_SUMMARY_CHARS}; failures fall back to the deterministic
 *   extractor so a consensus digest is still produced.
 */
import type { Context } from '@deepseek-ai/cordis';
import { type Message } from '@deepseek-ai/dsh-llm';
import type { Session } from '@deepseek-ai/dsh-session';
export declare const MAX_SUMMARY_CHARS = 600;
export declare const SUMMARY_TIMEOUT_MS = 30000;
export declare const SUMMARIZE_PROMPT: string;
export interface SummaryFields {
    readonly title: string | undefined;
    readonly objective: string;
    readonly resultText: string;
    readonly toolCount: number;
    readonly messageCount: number;
}
/** Deterministic fallback: bounded field concatenation, no LLM involved. */
export declare function deterministicSummary(fields: SummaryFields): string;
/** The last assembled prompt, captured by the watcher; the system text
 *  is derived with the same renderPrompt the agent loop uses, so the
 *  summarization request shares the session's exact system slot. */
export type AssemblySnapshot = import('@deepseek-ai/dsh-system-prompt').PromptAssembly;
/**
 * Live-session mode: session-derived prefix + trailing instruction.
 * The prefix is byte-identical to the session's own requests, so provider
 * prefix caches are reused; only the instruction + output are new tokens.
 */
export declare function summarizeLiveSession(options: {
    ctx: Context;
    session: Session;
    assembly: AssemblySnapshot;
    provider: string;
    model: string;
    maxTokens: number;
}): Promise<string>;
/**
 * Standalone mode: fixed template + extracted fields, no session context.
 * Used when the session is not live in this process (or the assembly
 * snapshot is missing). The fixed instruction prefix is itself cacheable.
 */
export declare function summarizeStandalone(options: {
    ctx: Context;
    fields: SummaryFields;
    provider: string;
    model: string;
    maxTokens: number;
}): Promise<string>;
export type { Message };
