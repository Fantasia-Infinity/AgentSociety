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
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { buildSharedContextSections, loadMirror, mirrorPath, } from './directory.js';
export const name = 'agent-society-directory-index';
export const inject = [];
export function apply(ctx) {
    const dshHome = resolve(process.env.DSH_HOME?.trim() || join(homedir(), '.dsh'));
    const path = mirrorPath(dshHome);
    ctx.on('system-prompt/assemble', async (assembly, _context, next) => {
        const assembled = await next();
        try {
            // The current session id keeps this session's own digests and row
            // out of the injected text: the digest watcher writes on every
            // ~60s idle gap, which would churn the prompt prefix each round.
            const current = _context.agent?.session?.id;
            const currentSessionId = typeof current === 'string' ? current : undefined;
            const sections = buildSharedContextSections(loadMirror(path), currentSessionId);
            if (sections.length === 0)
                return assembled;
            // Append as a RUNTIME CONTEXT (suffix position), never as a system
            // section: the request prefix (system sections + history + the
            // current user message) stays byte-identical forever, so cache
            // hits are preserved no matter how often the shared memory changes.
            // The snapshot itself lives at the tail, so its churn only ever
            // costs the small snapshot in cache terms.
            const contextText = sections
                .map((section) => section.text)
                .join('\n\n')
                // renderContextSections interpolates {{variables}}; shared-memory
                // text may contain brace pairs from code snippets, so neutralize
                // them without changing the visible content meaningfully.
                .replace(/\{\{/g, '{ {')
                .replace(/\}\}/g, '} }');
            const existing = new Set(assembled.contexts.map((context) => context.name));
            if (existing.has('agent-society:shared-context'))
                return assembled;
            return {
                ...assembled,
                contexts: [
                    ...assembled.contexts,
                    { name: 'agent-society:shared-context', text: contextText },
                ],
            };
        }
        catch (error) {
            ctx.logger.warn(`agent-society-directory-index failed; keeping the assembled prompt: ${error instanceof Error ? error.message : String(error)}`);
            return assembled;
        }
    }, { prepend: true });
}
//# sourceMappingURL=prompt-guard.js.map