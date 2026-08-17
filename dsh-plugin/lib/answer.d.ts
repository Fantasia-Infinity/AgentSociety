/**
 * Shared bounded question answering for AgentSociety.
 *
 * The standalone answering mode follows the prefix-invariance rule: the
 * answerer session's context is the model's own; the question is appended
 * via `followup`, and the answer is extracted from the assistant text.
 * Reused by the worker answerer and the interactive-process question bridge.
 */
import type { Context } from '@deepseek-ai/cordis';
import type { AgentSetup } from '@deepseek-ai/dsh-agent';
export declare const ANSWER_MARKER = "ANSWER:";
export declare const MAX_ANSWER_CHARS = 8000;
/** Pull the marked answer out of one assistant text (marker or whole text). */
export declare function extractAnswer(text: string): string;
export interface AnswerOptions {
    readonly provider: string;
    readonly model: string;
    readonly maxTokens?: number;
    readonly cwd: string;
    /** Tool setup for the answering session (defaults to no tool setup). */
    readonly setup: AgentSetup | undefined;
    /** Ask in conversation style, using the resumed session's context. */
    readonly inSession?: boolean;
}
/** Pick the best target session for default in-session answering. */
export declare function pickTargetSession(rows: readonly Record<string, unknown>[], options: {
    currentSessionId?: string;
    actorId?: string;
}): string | undefined;
/**
 * Answer one question with a fresh, tool-free one-shot session. The session
 * is disposed afterwards; failures reject so the caller can leave the
 * question leased for a later retry.
 */
export declare function answerQuestionWithSession(ctx: Context, question: string, options: AnswerOptions): Promise<string>;
/**
 * Answer a question INSIDE an existing session's context: resume the target
 * session (its history stays the prompt prefix, byte-identical), append the
 * question via followup, pull the ANSWER: text, and dispose the agent handle
 * without deleting the session. Failures reject so the caller can fall back.
 */
export declare function answerQuestionInSession(ctx: Context, question: string, options: AnswerOptions & {
    targetSessionId: string;
}): Promise<string>;
/** Directory row source used for default target resolution. */
export interface DirectoryRowSource {
    listDirectory(item: {
        after_seq?: number;
        query?: string;
        status?: string;
        actor_id?: string;
        limit?: number;
    }): Promise<Array<Record<string, unknown>>>;
}
export interface AnswerDispatchOptions extends AnswerOptions {
    readonly hub: DirectoryRowSource;
    readonly actorId: string;
    readonly currentSessionId?: string;
}
/**
 * Answer a question with the default target-session mode: an explicit
 * target_session_id wins; otherwise the target actor's most recent idle
 * continuous session is resumed and the question appended to its context
 * (prefix-invariance preserved). Any in-session failure falls back to the
 * one-shot answering session so the question never goes unanswered.
 */
export declare function answerQuestion(ctx: Context, question: {
    message?: unknown;
    target_session_id?: unknown;
}, options: AnswerDispatchOptions): Promise<string>;
