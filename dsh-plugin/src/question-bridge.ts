/**
 * AgentSociety question bridge for interactive dsh processes (TUI / Web).
 *
 * Runs whenever the bundle is loaded with Hub credentials, independent of
 * the worker flag. In auto mode (default) questions addressed to this
 * actor are answered with the bounded standalone answering session while no
 * human is present; when a human is present (or POLICY=ask) the questions
 * stay pending for the browser/TUI question card (P6a client plugin).
 *
 * The presence flag is a plain value: the browser client plugin reports UI
 * activity through the bridge's RPC surface; until a client connects, the
 * process counts as unattended (auto-answer) only when explicitly
 * configured, mirroring the worker behavior.
 */

import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/cordis-plugin-timer'
import Schema from '@deepseek-ai/schemastery'
import { homedir, hostname, userInfo } from 'node:os'
import { resolve } from 'node:path'

import { HubClient } from './hub-client.js'
import { answerQuestion } from './answer.js'

export const name = 'agent-society-question-bridge'
export const inject = ['timer']

export type QuestionPolicy = 'auto' | 'ask' | 'standalone'

export interface Config {
  hubUrl?: string
  hubTokenEnv?: string
  principalId?: string
  actorId?: string
  nodeId?: string
  workspaceRoot?: string
  provider?: string
  model?: string
  maxTokens?: number
  pollSeconds?: number
  /** auto | ask | standalone (AGENT_SOCIETY_QUESTION_POLICY). */
  policy?: QuestionPolicy
}

export const Config: Schema<Config> = Schema.object({
  hubUrl: Schema.string().required(false),
  hubTokenEnv: Schema.string().required(false),
  principalId: Schema.string().required(false),
  actorId: Schema.string().required(false),
  nodeId: Schema.string().required(false),
  workspaceRoot: Schema.string().required(false),
  provider: Schema.string().required(false),
  model: Schema.string().required(false),
  maxTokens: Schema.number().min(1).required(false),
  pollSeconds: Schema.number().min(2).default(10),
  policy: Schema.union(['auto', 'ask', 'standalone']).required(false),
})

export function apply(ctx: Context, config: Config): void {
  const hubUrl = config.hubUrl ?? process.env.AGENT_SOCIETY_HUB_URL?.trim()
  const tokenEnv = config.hubTokenEnv ?? 'AGENT_SOCIETY_HUB_TOKEN'
  const hubToken = process.env[tokenEnv]?.trim()
  if (!hubUrl || !hubToken) {
    ctx.logger.warn(
      'agent-society-question-bridge: Hub credentials are required; question bridge stays idle',
    )
    return
  }
  const owner = stableSlug(userInfo().username)
  const host = stableSlug(hostname())
  const principalId = config.principalId ?? `human-${owner}`
  const actorId = config.actorId ?? `agent-society-${host}`
  const nodeId = config.nodeId ?? host
  const workspaceRoot = resolve(config.workspaceRoot ?? process.cwd())
  const provider = config.provider ?? process.env.AGENT_SOCIETY_PROVIDER ?? 'deepseek-official'
  const model = config.model ?? process.env.DSH_MODEL ?? 'deepseek-v4-flash'
  const maxTokens = config.maxTokens
  const policy: QuestionPolicy =
    config.policy ??
    (process.env.AGENT_SOCIETY_QUESTION_POLICY as QuestionPolicy | undefined) ??
    'auto'
  const pollSeconds = config.pollSeconds ?? 10
  const hub = new HubClient(hubUrl, hubToken)

  const state = {
    /** Human presence reported by the browser card plugin (default: absent). */
    humanPresent: false,
    busy: false,
  }
  // Public surface for the client card (P6a): report presence and read the
  // pending questions. Plain values; the browser half calls these over the
  // host RPC bridge once wired. `reflect` is an optional host service.
  const reflect = ctx.get('reflect') as
    | { provide(name: string, value: unknown): unknown }
    | undefined
  reflect?.provide('agentSocietyQuestionBridge', {
    setHumanPresent(present: boolean): void {
      state.humanPresent = present
    },
    humanPresent(): boolean {
      return state.humanPresent
    },
    policy,
    actorId,
    nodeId,
  })

  const timer = ctx.setInterval(() => {
    void poll()
  }, pollSeconds * 1_000)
  ctx.effect(() => () => timer())
  void poll()

  async function poll(): Promise<void> {
    if (state.busy) return
    if (policy === 'ask') return // the human-facing card handles everything
    const autoAnswer = policy === 'standalone' || !state.humanPresent
    if (!autoAnswer) return
    state.busy = true
    try {
      const questions = await hub.claimQuestions({
        actor_id: actorId,
        node_id: nodeId,
        limit: 3,
      })
      for (const question of questions) {
        const questionId = String(question.question_id)
        const leaseToken = String(question.lease_token)
        try {
          const answer = await answerQuestion(ctx, question, {
            provider,
            model,
            ...(maxTokens === undefined ? {} : { maxTokens }),
            cwd: workspaceRoot,
            setup: undefined,
            hub,
            actorId,
          })
          await hub.answerQuestion(questionId, {
            lease_token: leaseToken,
            answer_text: answer,
          })
          ctx.logger.info(
            `agent-society-question-bridge answered ${questionId}`,
          )
        } catch (error) {
          ctx.logger.warn(
            `agent-society-question-bridge could not answer ${questionId}: ${error instanceof Error ? error.message : String(error)}`,
          )
        }
      }
    } catch (error) {
      ctx.logger.warn(
        `agent-society-question-bridge poll failed: ${error instanceof Error ? error.message : String(error)}`,
      )
    } finally {
      state.busy = false
    }
  }
}

function stableSlug(value: string): string {
  const slug = value.toLowerCase().replace(/[^a-z0-9._-]+/gu, '-')
  return slug.replace(/^-+|-+$/gu, '') || 'node'
}
