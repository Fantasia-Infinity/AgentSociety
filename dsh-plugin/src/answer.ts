/**
 * Shared bounded question answering for AgentSociety.
 *
 * The standalone answering mode follows the prefix-invariance rule: the
 * answerer session's context is the model's own; the question is appended
 * via `followup`, and the answer is extracted from the assistant text.
 * Reused by the worker answerer and the interactive-process question bridge.
 */

import type { Context } from '@deepseek-ai/cordis'
import type { AgentSetup } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import { randomUUID } from 'node:crypto'

export const ANSWER_MARKER = 'ANSWER:'
export const MAX_ANSWER_CHARS = 8_000

/** Pull the marked answer out of one assistant text (marker or whole text). */
export function extractAnswer(text: string): string {
  const markerIndex = text.indexOf(ANSWER_MARKER)
  const answer = markerIndex >= 0
    ? text.slice(markerIndex + ANSWER_MARKER.length).trim()
    : text.trim()
  return answer.slice(0, MAX_ANSWER_CHARS)
}

export interface AnswerOptions {
  readonly provider: string
  readonly model: string
  readonly maxTokens?: number
  readonly cwd: string
  /** Tool setup for the answering session (defaults to no tool setup). */
  readonly setup: AgentSetup | undefined
  /** Ask in conversation style, using the resumed session's context. */
  readonly inSession?: boolean
}

/** Pick the best target session for default in-session answering. */
export function pickTargetSession(
  rows: readonly Record<string, unknown>[],
  options: { currentSessionId?: string; actorId?: string },
): string | undefined {
  const candidates = rows
    .filter((row) => {
      const sessionId = row["session_id"]
      return (
        typeof sessionId === "string" &&
        sessionId.length > 0 &&
        sessionId !== options.currentSessionId &&
        row["status"] !== "working" &&
        (options.actorId === undefined || row["actor_id"] === options.actorId)
      )
    })
    .sort((a, b) => Number(b["last_active_at"] ?? 0) - Number(a["last_active_at"] ?? 0))
  // Continuous long-lived sessions first; fall back to any recent idle one.
  const continuous = candidates.find((row) => row["session_mode"] === "continuous")
  return String((continuous ?? candidates[0])?.["session_id"] ?? "")
    || undefined
}

/**
 * Answer one question with a fresh, tool-free one-shot session. The session
 * is disposed afterwards; failures reject so the caller can leave the
 * question leased for a later retry.
 */
export async function answerQuestionWithSession(
  ctx: Context,
  question: string,
  options: AnswerOptions,
): Promise<string> {
  const sessionId = `agent-society-question-${randomUUID().replaceAll('-', '')}`
  const handle = await ctx.agents.create({
    sessionId: SessionId(sessionId),
    agentOptions: {
      provider: options.provider,
      model: options.model,
      ...(options.maxTokens === undefined
        ? {}
        : { maxTokens: Math.min(options.maxTokens, 2048) }),
    },
    meta: { cwd: options.cwd },
    ...(options.setup === undefined ? {} : { setup: options.setup }),
  })
  try {
    const agent = handle.agent
    agent.followup(
      createUserMessage({
        content: [{
          type: 'text',
          text:
            'Answer the question below concisely and factually, using only ' +
            'your own knowledge. Reply with exactly one text block, ' +
            `format: ${ANSWER_MARKER} <text>\n\nQuestion: ${question}`,
        }],
        source: { kind: 'user' },
      }),
    )
    await agent.whenIdle()
    const text = lastAssistantText(agent)
    if (!text) throw new Error('answering session ended without an assistant message')
    return extractAnswer(text)
  } finally {
    await handle.dispose()
  }
}

/** Last assistant text block of a session (shared by both answer paths). */
function lastAssistantText(agent: { session: { events: readonly unknown[] } }): string {
  for (let index = agent.session.events.length - 1; index >= 0; index -= 1) {
    const event = agent.session.events[index] as
      | { type?: string; data?: { message?: { content?: unknown } } }
      | undefined
    if (!event || event.type !== 'assistant/message') continue
    const content = event.data?.message?.content
    if (!Array.isArray(content)) continue
    const block = content
      .filter(
        (item): item is Extract<typeof content[number], { type: 'text' }> =>
          typeof item === 'object' && item !== null && (item as { type?: string }).type === 'text',
      )
      .map((item) => (item as { text?: string }).text ?? '')
      .join('')
      .trim()
    if (block) return block
  }
  return ''
}

/**
 * Answer a question INSIDE an existing session's context: resume the target
 * session (its history stays the prompt prefix, byte-identical), append the
 * question via followup, pull the ANSWER: text, and dispose the agent handle
 * without deleting the session. Failures reject so the caller can fall back.
 */
export async function answerQuestionInSession(
  ctx: Context,
  question: string,
  options: AnswerOptions & { targetSessionId: string },
): Promise<string> {
  const handle = await ctx.agents.resume({
    resumeSessionId: SessionId(options.targetSessionId),
    agentOptions: {
      provider: options.provider,
      model: options.model,
      ...(options.maxTokens === undefined
        ? {}
        : { maxTokens: Math.min(options.maxTokens, 2048) }),
    },
    ...(options.setup === undefined ? {} : { setup: options.setup }),
  })
  try {
    const agent = handle.agent
    agent.followup(
      createUserMessage({
        content: [{
          type: 'text',
          text:
            'Using the conversation context above, answer the question below ' +
            'concisely and factually. Reply with exactly one text block, ' +
            `format: ${ANSWER_MARKER} <text>\n\nQuestion: ${question}`,
        }],
        source: { kind: 'user' },
      }),
    )
    await agent.whenIdle()
    const text = lastAssistantText(agent)
    if (!text) throw new Error('answering session ended without an assistant message')
    return extractAnswer(text)
  } finally {
    await handle.dispose()
  }
}

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
export async function answerQuestion(
  ctx: Context,
  question: { message?: unknown; target_session_id?: unknown },
  options: AnswerDispatchOptions,
): Promise<string> {
  const text = String(question.message ?? '')
  const explicit =
    typeof question.target_session_id === 'string' &&
    question.target_session_id.length > 0
      ? question.target_session_id
      : undefined
  const targetSessionId =
    explicit ??
    (await resolveDefaultTarget(options))
  if (targetSessionId !== undefined) {
    try {
      return await answerQuestionInSession(ctx, text, {
        ...options,
        targetSessionId,
      })
    } catch (error) {
      ctx.logger.warn(
        `agent-society in-session answer to ${targetSessionId} failed, falling back to a one-shot session: ${error instanceof Error ? error.message : String(error)}`,
      )
    }
  }
  return answerQuestionWithSession(ctx, text, options)
}

/** Default mode: resume the target actor's most recent idle session. */
async function resolveDefaultTarget(
  options: AnswerDispatchOptions,
): Promise<string | undefined> {
  try {
    const rows = await options.hub.listDirectory({
      actor_id: options.actorId,
      limit: 100,
    })
    return pickTargetSession(rows, {
      ...(options.currentSessionId === undefined
        ? {}
        : { currentSessionId: options.currentSessionId }),
      ...(options.actorId === undefined ? {} : { actorId: options.actorId }),
    })
  } catch (error) {
    options.hub ?? undefined
    return undefined
  }
}
