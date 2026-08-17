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
}
/**
 * Answer one question with a fresh, tool-free one-shot session. The session
 * is disposed afterwards; failures reject so the caller can leave the
 * question leased for a later retry.
 */
export declare function answerQuestionWithSession(ctx: Context, question: string, options: AnswerOptions): Promise<string>;
