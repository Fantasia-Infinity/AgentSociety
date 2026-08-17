/**
 * Session directory helpers shared by the worker (invocation upserts) and
 * the `agent-society-directory` plugin (local-session sync + mirror cache).
 *
 * The mirror (`~/.dsh/agent-society-directory.json`, 0600) is a local cache
 * of the Hub's per-principal directory: own sessions keep full depth-0/1
 * rows, other sessions keep their latest pushed row. It is derived state —
 * the Hub log remains authoritative.
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
export const MIRROR_MAX_INVOCATIONS = 10;
export function mirrorPath(dshHome) {
    return join(dshHome, 'agent-society-directory.json');
}
export function loadMirror(path) {
    try {
        const parsed = JSON.parse(readFileSync(path, 'utf8'));
        if (parsed !== null &&
            typeof parsed === 'object' &&
            !Array.isArray(parsed) &&
            typeof parsed.rows === 'object') {
            const record = parsed;
            const rows = record.rows;
            const normalized = {};
            for (const [sessionId, row] of Object.entries(rows)) {
                if (row !== null && typeof row === 'object' && typeof row.session_id === 'string') {
                    normalized[sessionId] = row;
                }
            }
            return {
                seq: typeof record.seq === 'number' ? record.seq : 0,
                updated_at: typeof record.updated_at === 'number' ? record.updated_at : 0,
                rows: normalized,
            };
        }
    }
    catch {
        // Missing or partial mirror is a normal first run.
    }
    return { seq: 0, updated_at: 0, rows: {} };
}
export function saveMirror(path, mirror) {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    writeFileSync(path, `${JSON.stringify(mirror, null, 2)}\n`, {
        encoding: 'utf8',
        mode: 0o600,
    });
}
/** Merge one invocation into the row for a session (bounded history). */
export function mergeInvocation(options) {
    const base = options.row ?? {
        session_id: options.sessionId,
        workspace: options.workspace,
        status: 'idle',
        last_active_at: options.invocation.at,
        session_mode: options.sessionMode,
        tool_policy: options.toolPolicy,
        invocations: [],
    };
    const invocations = [options.invocation, ...base.invocations]
        .filter((item, index, all) => all.findIndex((other) => other.run_id === item.run_id) === index)
        .slice(0, MIRROR_MAX_INVOCATIONS);
    return {
        ...base,
        session_id: options.sessionId,
        ...(options.title ? { title: options.title } : {}),
        workspace: options.workspace,
        status: options.invocation.status === 'completed' ? 'done' : 'failed',
        last_active_at: options.invocation.at,
        invocations,
    };
}
/** Build a depth-0/1 row for a local session from its persistence header. */
export function buildLocalRow(options) {
    return {
        session_id: options.sessionId,
        ...(options.title ? { title: options.title } : {}),
        workspace: options.workspace,
        status: 'idle',
        last_active_at: options.lastActiveAt,
        session_mode: options.sessionMode,
        tool_policy: options.toolPolicy,
        invocations: [],
    };
}
/** Read session titles from the dsh projection cache when present. */
export function loadProjectionTitles(dshHome) {
    const titles = new Map();
    try {
        const cache = JSON.parse(readFileSync(join(dshHome, 'storages', 'session_projcache.json'), 'utf8'));
        const sessions = cache.tables?.sessions;
        if (!sessions)
            return titles;
        for (const [sessionId, record] of Object.entries(sessions)) {
            const title = record?.rows?.title?.val;
            if (typeof title === 'string' && title.length > 0) {
                titles.set(sessionId, title);
            }
        }
    }
    catch {
        // No projection cache is a normal cold start.
    }
    return titles;
}
//# sourceMappingURL=directory.js.map