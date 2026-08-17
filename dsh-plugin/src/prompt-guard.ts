/**
 * `agent-society-directory-index`: bounded shared-context injection guard.
 *
 * Runs after the ordinary system-prompt assembly (prepend listener) and
 * appends two bounded sections — the consensus one-liners and the session/
 * agent directory index — read from the local mirror maintained by the
 * directory sync plugin. The combined text stays under 4KB; on any failure
 * the original assembly is returned unchanged (same fail-safe philosophy as
 * the tool guards).
 *
 * KV-cache contract: the injection only APPENDS to the assembled sections
 * (the core system prefix is never touched), and the rendered text changes
 * only on semantic events — a new consensus digest or a session status/title
 * change. Idle churn (mirror refreshes, last_active_at) must not alter the
 * bytes; directory rows therefore sort stably by session_id. The section is
 * model-facing only: it is never written into the session log, so no UI
 * surface renders it as a user-visible message.
 */

import type { Context } from '@deepseek-ai/cordis'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'

import {
  buildSharedContextSections,
  loadMirror,
  mirrorPath,
} from './directory.js'

export const name = 'agent-society-directory-index'
export const inject: string[] = []

export function apply(ctx: Context): void {
  const dshHome = resolve(process.env.DSH_HOME?.trim() || join(homedir(), '.dsh'))
  const path = mirrorPath(dshHome)

  ctx.on(
    'system-prompt/assemble',
    async (assembly, _context, next) => {
      const assembled = await next()
      try {
        // The current session id keeps this session's own digests and row
        // out of the injected text: the digest watcher writes on every
        // ~60s idle gap, which would churn the prompt prefix each round.
        const current = (_context as {
          agent?: { session?: { id?: unknown } }
        }).agent?.session?.id
        const currentSessionId = typeof current === 'string' ? current : undefined
        const sections = buildSharedContextSections(loadMirror(path), currentSessionId)
        if (sections.length === 0) return assembled
        const existing = new Set(assembled.sections.map((section) => section.name))
        const additions = sections.filter((section) => !existing.has(section.name))
        if (additions.length === 0) return assembled
        return {
          ...assembled,
          sections: [...assembled.sections, ...additions],
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
