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
    let text = ''
    for (let index = agent.session.events.length - 1; index >= 0; index -= 1) {
      const event = agent.session.events[index]
      if (!event || event.type !== 'assistant/message') continue
      const content = event.data.message.content
      const block = content
        .filter(
          (item): item is Extract<(typeof content)[number], { type: 'text' }> =>
            item.type === 'text',
        )
        .map((item) => item.text)
        .join('')
        .trim()
      if (block) {
        text = block
        break
      }
    }
    if (!text) throw new Error('answering session ended without an assistant message')
    return extractAnswer(text)
  } finally {
    await handle.dispose()
  }
}
