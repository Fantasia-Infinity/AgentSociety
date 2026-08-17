/**
 * `agent-society-directory-index`: bounded shared-context injection guard.
 *
 * Runs after the ordinary system-prompt assembly (prepend listener) and
 * appends two bounded sections — the consensus one-liners and the session/
 * agent directory index — read from the local mirror maintained by the
 * directory sync plugin. The combined text stays under 4KB; on any failure
 * the original assembly is returned unchanged (same fail-safe philosophy as
 * the tool guards).
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
            const sections = buildSharedContextSections(loadMirror(path));
            if (sections.length === 0)
                return assembled;
            const existing = new Set(assembled.sections.map((section) => section.name));
            const additions = sections.filter((section) => !existing.has(section.name));
            if (additions.length === 0)
                return assembled;
            return {
                ...assembled,
                sections: [...assembled.sections, ...additions],
            };
        }
        catch (error) {
            ctx.logger.warn(`agent-society-directory-index failed; keeping the assembled prompt: ${error instanceof Error ? error.message : String(error)}`);
            return assembled;
        }
    }, { prepend: true });
}
//# sourceMappingURL=prompt-guard.js.map