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
export const MIRROR_MAX_CONSENSUS_ENTRIES = 24;
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
            let consensus = { seq: 0, entries: [] };
            const rawConsensus = record.consensus;
            if (rawConsensus !== null &&
                typeof rawConsensus === 'object' &&
                Array.isArray(rawConsensus.entries)) {
                consensus = {
                    seq: typeof rawConsensus.seq === 'number' ? rawConsensus.seq : 0,
                    entries: rawConsensus.entries.filter((entry) => entry !== null &&
                        typeof entry === 'object' &&
                        typeof entry.kind === 'string' &&
                        typeof entry.summary === 'string'),
                };
            }
            return {
                seq: typeof record.seq === 'number' ? record.seq : 0,
                updated_at: typeof record.updated_at === 'number' ? record.updated_at : 0,
                rows: normalized,
                consensus,
            };
        }
    }
    catch {
        // Missing or partial mirror is a normal first run.
    }
    return { seq: 0, updated_at: 0, rows: {}, consensus: { seq: 0, entries: [] } };
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
        ...(options.actorId ? { actor_id: options.actorId } : {}),
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
        actor_id: options.actorId,
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
/** Projection-cache activity: title and lastPromptAt (ms epoch) per session. */
export function loadProjectionActivity(dshHome) {
    const activity = new Map();
    try {
        const cache = JSON.parse(readFileSync(join(dshHome, 'storages', 'session_projcache.json'), 'utf8'));
        const sessions = cache.tables?.sessions;
        if (!sessions)
            return activity;
        for (const [sessionId, record] of Object.entries(sessions)) {
            const rows = record?.rows;
            const title = rows?.title?.val;
            const lastPromptAt = rows?.sessionListMetadata?.val?.lastPromptAt;
            const entry = {};
            if (typeof title === 'string' && title.length > 0)
                entry.title = title;
            if (typeof lastPromptAt === 'number' && Number.isFinite(lastPromptAt)) {
                entry.lastPromptAt = lastPromptAt;
            }
            if (entry.title !== undefined || entry.lastPromptAt !== undefined) {
                activity.set(sessionId, entry);
            }
        }
    }
    catch {
        // No projection cache is a normal cold start.
    }
    return activity;
}
// ── Prompt injection (bounded) ────────────────────────────────────────────
export const DIRECTORY_INDEX_SECTION = 'agent-society:directory-index';
export const CONSENSUS_SECTION = 'agent-society:consensus';
export const PROMPT_BUDGET_CHARS = 4_000;
const MAX_CONSENSUS_LINES = 8;
const MAX_DIRECTORY_LINES = 20;
/** One-line consensus summaries for the prompt (most recent first).
 *
 * KV-cache stability: this session's own digests are excluded. The digest
 * watcher writes a new entry whenever the session idles ~60s, so an active
 * conversation would otherwise change the injected bytes every round and
 * break the request prefix (cache miss) — while adding nothing the model
 * does not already have in the transcript.
 */
export function consensusPromptLines(mirror, currentSessionId) {
    const lines = [];
    const seen = new Set();
    for (const entry of mirror.consensus.entries) {
        if (entry.session_id === currentSessionId)
            continue;
        // The digest watcher writes one entry per ~60s idle gap, so an active
        // remote session can produce several entries with identical text. Keep
        // the newest copy of each (session, summary) pair so the bounded 8-line
        // budget is spent on distinct facts, not repeated "OK" digests.
        const key = `${entry.session_id ?? ''}|${entry.summary}`;
        if (seen.has(key))
            continue;
        seen.add(key);
        const where = entry.session_id ? ` ${entry.session_id}` : '';
        lines.push(`- [${entry.kind}]${where}: ${entry.summary}`);
        if (lines.length >= MAX_CONSENSUS_LINES)
            break;
    }
    return lines;
}
/** Ranked directory index lines: working > recently active > recent rows. */
export function directoryPromptLines(mirror, currentSessionId) {
    const rows = Object.values(mirror.rows).filter((row) => row.session_id !== currentSessionId);
    const rank = (row) => (row.status === 'working' ? 0 : 1);
    // KV-cache stability: rank first, then session_id (stable byte order).
    // last_active_at changes on every mirror pull and would reorder rows
    // between assemblies, churning the prompt prefix for no semantic gain.
    // Status/title changes still update the text, but idle churn must not.
    rows.sort((a, b) => rank(a) - rank(b) ||
        (a.session_id < b.session_id ? -1 : a.session_id > b.session_id ? 1 : 0));
    return rows.slice(0, MAX_DIRECTORY_LINES).map((row) => {
        const label = row.title && row.title.trim()
            ? row.title.trim()
            : row.workspace || 'unknown workspace';
        return `- ${row.session_id} | ${row.actor_id ?? '?'} | ${label} | ${row.status}`;
    });
}
/**
 * Build the two bounded prompt sections for one assembly. Total text stays
 * under {@link PROMPT_BUDGET_CHARS}; entries are dropped from the directory
 * index first, then the consensus list, until the budget fits.
 */
export function buildSharedContextSections(mirror, currentSessionId) {
    const consensusLines = consensusPromptLines(mirror, currentSessionId);
    const directoryLines = directoryPromptLines(mirror, currentSessionId);
    const build = (consensus, directory) => {
        const sections = [];
        if (consensus.length > 0) {
            sections.push({
                name: CONSENSUS_SECTION,
                text: '## 共享共识上下文（AgentSociety）\n' +
                    consensus.join('\n') +
                    '\n（需要跨设备信息或历史结论时，先 hub_context_read 查共享记忆）',
            });
        }
        if (directory.length > 0) {
            sections.push({
                name: DIRECTORY_INDEX_SECTION,
                text: '## 会话/Agent 目录（AgentSociety）\n' +
                    directory.join('\n') +
                    '\n（需要找到相关会话/向谁求助时：先 hub_directory_search/get 下钻；' +
                    '仍无答案再 hub_ask 提问；任务中得出可复用结论时 hub_context_append 写回）',
            });
        }
        return sections;
    };
    let sections = build(consensusLines, directoryLines);
    let total = sections.reduce((sum, section) => sum + section.text.length, 0);
    while (total > PROMPT_BUDGET_CHARS && directoryLines.length > 0) {
        directoryLines.pop();
        sections = build(consensusLines, directoryLines);
        total = sections.reduce((sum, section) => sum + section.text.length, 0);
    }
    while (total > PROMPT_BUDGET_CHARS && consensusLines.length > 0) {
        consensusLines.pop();
        sections = build(consensusLines, directoryLines);
        total = sections.reduce((sum, section) => sum + section.text.length, 0);
    }
    return sections;
}
//# sourceMappingURL=directory.js.map