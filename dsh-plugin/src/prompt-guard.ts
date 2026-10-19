/**
 * `agent-society-directory-index`: bounded shared-context injection guard.
 *
 * `balanced` (the default) adds one small initial snapshot and then only
 * semantic deltas as durable user messages through the public `agent/pre-step`
 * waterfall. `legacy` keeps the old runtime-context snapshot for rollback;
 * `off` keeps only the static workflow guide. No mode rewrites the dsh
 * surface or starts a new request series.
 *
 * KV-cache contract: the system prompt stays static and balanced updates are
 * append-only messages. Idle mirror refreshes and last_active_at changes do
 * not produce a message; full objective/result data remains available through
 * Hub MCP on demand.
 */

import type { Context } from '@deepseek-ai/cordis'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    'agent-society-directory-index': { kind: 'agent-society-directory-index' }
  }
}
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'

import {
  buildSharedContextSections,
  loadMirror,
  mirrorPath,
  normalizeSharedContextState,
  renderSharedContextUpdate,
  WORKFLOW_GUIDE,
} from './directory.js'
import { recordAgentSocietyMetrics } from './context-metrics.js'

export const name = 'agent-society-directory-index'
export const inject: string[] = []

export type SharedContextMode = 'balanced' | 'legacy' | 'off'

export function sharedContextMode(
  env: NodeJS.ProcessEnv = process.env,
): SharedContextMode {
  if (env.AGENT_SOCIETY_DIRECTORY === '0') return 'off'
  const value = env.AGENT_SOCIETY_CONTEXT_MODE?.trim().toLowerCase()
  return value === 'legacy' || value === 'off' ? value : 'balanced'
}

export function apply(ctx: Context): void {
  const dshHome = resolve(process.env.DSH_HOME?.trim() || join(homedir(), '.dsh'))
  const path = mirrorPath(dshHome)
  const mode = sharedContextMode()

  // The workflow guide is static and lives in a system section: the model
  // always knows how to reach shared memory, even on turns where the
  // change-detected data snapshot is not re-injected.
  const systemPrompt = ctx.get('systemPrompt') as
    | { section(input: { name: string; order: number; text: string }): unknown }
    | undefined
  if (systemPrompt !== undefined) {
    systemPrompt.section({
      name: 'agent-society:workflow',
      order: 199,
      text: WORKFLOW_GUIDE,
    })
  }

  if (mode === 'balanced') {
    // State is process-local because the durable session already owns the
    // accepted messages. A new process seeing a session starts with one
    // bounded snapshot, then follows semantic changes in that process.
    const lastState = new Map<string, ReturnType<typeof normalizeSharedContextState>>()

    ctx.on('agent/pre-step', async (payload, next) => {
      const decision = await next()
      if (decision.kind !== 'enter') return decision
      try {
        const sessionId = String(payload.agent.session.id)
        const current = normalizeSharedContextState(loadMirror(path), sessionId)
        const previous = lastState.get(sessionId)
        const update = renderSharedContextUpdate(previous, current)
        // Record an already-seen semantic state even when no message is
        // needed, so sequence-only mirror refreshes never become future
        // deltas. An empty first sight is not recorded: the first real data
        // that arrives should be introduced as an initial snapshot.
        if (previous !== undefined || update !== undefined) {
          lastState.set(sessionId, current)
        }
        if (lastState.size > 1_024) {
          const first = lastState.keys().next().value
          if (typeof first === 'string') lastState.delete(first)
        }
        if (update === undefined) return decision
        recordAgentSocietyMetrics(dshHome, {
          context: {
            kind: update.kind,
            bytes: Buffer.byteLength(update.text, 'utf8'),
            sessionId,
          },
        })
        return {
          ...decision,
          messages: [
            ...decision.messages,
            createUserMessage({
              content: [{ type: 'text', text: update.text }],
              source: {
                kind: 'agent-society-directory-index',
              },
            }),
          ],
        }
      } catch (error) {
        ctx.logger.warn(
          `agent-society-directory-index balanced update failed; keeping the proposed step: ${error instanceof Error ? error.message : String(error)}`,
        )
        return decision
      }
    })
    return
  }

  if (mode === 'off') return

  // Legacy mode: retain the former bounded runtime-context projection for
  // rollback and A/B comparison. It is intentionally not used by balanced.
  const lastInjected = new Map<string, string>()

  ctx.on(
    'system-prompt/assemble',
    async (assembly, _context, next) => {
      const assembled = await next()
      try {
        const current = (_context as {
          agent?: { session?: { id?: unknown } }
        }).agent?.session?.id
        const currentSessionId = typeof current === 'string' ? current : undefined
        const sections = buildSharedContextSections(loadMirror(path), currentSessionId)
        if (sections.length === 0) return assembled
        // Append as a RUNTIME CONTEXT (suffix position), never as a system
        // section: the request prefix (system sections + history + the
        // current user message) stays byte-identical forever, so cache
        // hits are preserved no matter how often the shared memory changes.
        const contextText = sections
          .map((section) => section.text)
          .join('\n\n')
          // renderContextSections interpolates {{variables}}; shared-memory
          // text may contain brace pairs from code snippets, so neutralize
          // them without changing the visible content meaningfully.
          .replace(/\{\{/g, '{ {')
          .replace(/\}\}/g, '} }')
        const key = currentSessionId ?? '__global__'
        if (lastInjected.get(key) === contextText) return assembled
        lastInjected.set(key, contextText)
        const existing = new Set(assembled.contexts.map((context) => context.name))
        if (existing.has('agent-society:shared-context')) return assembled
        return {
          ...assembled,
          contexts: [
            ...assembled.contexts,
            { name: 'agent-society:shared-context', text: contextText },
          ],
        }
      } catch (error) {
        ctx.logger.warn(
          `agent-society-directory-index failed; keeping the assembled prompt: ${error instanceof Error ? error.message : String(error)}`,
        )
        return assembled
      }
    },
    { prepend: true },
  )
}
