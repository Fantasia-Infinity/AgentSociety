/**
 * Interactive-session consensus digest watcher.
 *
 * Runs inside every hub-connected dsh process (TUI / Web / worker). For
 * each LIVE session in this process (the ones a human is actually talking
 * to), it detects the end of an interaction round — new messages followed
 * by an idle gap — and appends a consensus digest, summarized by the LLM
 * by default (KV-cache-friendly: session-derived prefix + trailing
 * instruction, see summarizer.ts) with a deterministic fallback.
 *
 * Anti-repetition guarantees (no LLM calls unless warranted):
 * - zero new content => zero LLM calls (pure count comparison);
 * - one digest per round: a persisted watermark per session
 *   (~/.dsh/agent-society-digest-state.json, 0600) survives restarts;
 * - in-flight guard prevents re-entry; bounded retries (3) then give up;
 * - idempotent event_id (principal|consensus|digest|session|round count),
 *   so even a cross-process duplicate cannot duplicate the Hub entry;
 * - `agent-society-*` task sessions are skipped: the worker task path
 *   already writes their digests.
 */
import Schema from '@deepseek-ai/schemastery';
import { createHash } from 'node:crypto';
import { homedir, hostname, userInfo } from 'node:os';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { HubClient } from './hub-client.js';
import { loadProjectionActivity } from './directory.js';
import { buildSessionDigest } from './digest.js';
import { deterministicSummary, summarizeLiveSession, summarizeStandalone, } from './summarizer.js';
export const name = 'agent-society-session-digest';
export const inject = ['timer'];
export const Config = Schema.object({
    hubUrl: Schema.string().required(false),
    hubTokenEnv: Schema.string().required(false),
    principalId: Schema.string().required(false),
    actorId: Schema.string().required(false),
    nodeId: Schema.string().required(false),
    workspaceRoot: Schema.string().required(false),
    provider: Schema.string().required(false),
    model: Schema.string().required(false),
    maxTokens: Schema.number().min(1).required(false),
    pollSeconds: Schema.number().min(5).default(15),
    idleSeconds: Schema.number().min(10).default(60),
    summarize: Schema.boolean().required(false),
    contextEnabled: Schema.boolean().required(false),
});
const TASK_SESSION_PREFIX = 'agent-society-';
const MAX_ATTEMPTS = 3;
export function apply(ctx, config) {
    const hubUrl = config.hubUrl ?? process.env.AGENT_SOCIETY_HUB_URL?.trim();
    const tokenEnv = config.hubTokenEnv ?? 'AGENT_SOCIETY_HUB_TOKEN';
    const hubToken = process.env[tokenEnv]?.trim();
    const contextEnabled = config.contextEnabled ?? process.env.AGENT_SOCIETY_CONTEXT !== '0';
    if (!hubUrl || !hubToken || !contextEnabled) {
        ctx.logger.info(`agent-society-session-digest idle (hub=${Boolean(hubUrl)} token=${Boolean(hubToken)} context=${contextEnabled})`);
        return;
    }
    const owner = stableSlug(userInfo().username);
    const host = stableSlug(hostname());
    const principalId = config.principalId ?? `human-${owner}`;
    const actorId = config.actorId ?? `agent-society-${host}`;
    const nodeId = config.nodeId ?? host;
    const workspaceRoot = resolve(config.workspaceRoot ?? process.cwd());
    const provider = config.provider ?? process.env.AGENT_SOCIETY_PROVIDER ?? 'deepseek-official';
    const model = config.model ?? process.env.DSH_MODEL ?? 'deepseek-v4-flash';
    const maxTokens = Math.min(config.maxTokens ?? 2048, 2048);
    const pollSeconds = config.pollSeconds ?? 15;
    const idleSeconds = config.idleSeconds ?? 60;
    const summarize = config.summarize ?? process.env.AGENT_SOCIETY_CONTEXT_SUMMARIZE !== '0';
    ctx.logger.warn(`agent-society-session-digest active (hub=${hubUrl}, summarize=${summarize}, poll=${pollSeconds}s, idle=${idleSeconds}s)`);
    // DIAGNOSTIC PROBE — remove after verification
    try {
        writeFileSync('/tmp/watcher-probe.txt', `alive ${Date.now()}\n`, { flag: 'a' });
    }
    catch { /* ignore */ }
    const hub = new HubClient(hubUrl, hubToken);
    const statePath = resolve(process.env.DSH_HOME?.trim() || resolve(homedir(), '.dsh'), 'agent-society-digest-state.json');
    const state = loadState(statePath);
    const inFlight = new Set();
    const attempts = new Map();
    // Capture the latest assembly so live-session summaries can reuse the
    // exact system prompt (renderPrompt) and tool schemas the session's own
    // requests used — the summarization request then shares the session's
    // byte-identical prefix.
    let assembly;
    ctx.on('system-prompt/assemble', async (assembledInput, _context, next) => {
        const assembled = await next();
        assembly = assembled;
        return assembled;
    }, { prepend: true });
    const probe = (message) => {
        try {
            writeFileSync('/tmp/watcher-probe.txt', `${message} ${Date.now()}\n`, { flag: 'a' });
        }
        catch { /* ignore */ }
    };
    const timer = ctx.setInterval(() => {
        void tick();
    }, pollSeconds * 1_000);
    ctx.effect(() => () => timer());
    void tick();
    async function tick() {
        const sessions = ctx.get('sessions');
        const persistence = ctx.get('sessionPersistence');
        const now = Date.now();
        const liveIds = new Set();
        probe(`tick sessions=${Boolean(sessions)} persistence=${Boolean(persistence)}`);
        ctx.logger.debug(`agent-society-session-digest tick (sessions=${Boolean(sessions)}, persistence=${Boolean(persistence)})`);
        // Path 1: live sessions in THIS process (best KV-cache behaviour:
        // session-derived prefix + trailing instruction).
        if (sessions && typeof sessions.list === 'function') {
            for (const session of sessions.list()) {
                if (!session || typeof session.id !== 'string')
                    continue;
                if (session.id.startsWith(TASK_SESSION_PREFIX))
                    continue;
                liveIds.add(session.id);
                await handleRound(session.id, session.events ?? [], { live: true });
            }
        }
        // Path 2: persisted sessions not live here (the web may recreate agent
        // fibers per turn, so between turns the store can be empty). Uses the
        // projection cache's lastPromptAt as the activity watermark and
        // standalone summarization (no session context copy).
        if (persistence && typeof persistence.list === 'function') {
            try {
                const headers = await persistence.list();
                const activity = loadProjectionActivity(process.env.DSH_HOME?.trim() || resolve(homedir(), '.dsh'));
                for (const header of headers) {
                    if (!header || typeof header.id !== 'string')
                        continue;
                    if (header.id.startsWith(TASK_SESSION_PREFIX))
                        continue;
                    if (liveIds.has(header.id))
                        continue;
                    const info = activity.get(header.id);
                    const lastPromptAt = info?.lastPromptAt;
                    if (lastPromptAt === undefined) {
                        ctx.logger.debug(`agent-society-session-digest ${header.id}: no lastPromptAt in projection cache`);
                        continue;
                    }
                    const prior = state[header.id];
                    if (prior !== undefined && prior.lastPromptAt !== undefined && lastPromptAt <= prior.lastPromptAt)
                        continue;
                    // A round is over only after an idle gap since the last prompt.
                    if (now - lastPromptAt < idleSeconds * 1_000)
                        continue;
                    if (inFlight.has(header.id))
                        continue;
                    if (attempts.get(header.id) ?? 0 >= MAX_ATTEMPTS)
                        continue;
                    probe(`hit ${header.id} lastPromptAt=${lastPromptAt}`);
                    inFlight.add(header.id);
                    try {
                        const inspection = await persistence.inspect(header.id);
                        const count = (inspection.events ?? []).length;
                        const summary = await summarizeFor({ id: header.id, events: inspection.events }, count, { live: false, ...(info === undefined ? {} : { title: info.title }) });
                        const digest = buildSessionDigest({
                            principalId,
                            sessionId: header.id,
                            actorId,
                            nodeId,
                            title: summary.title,
                            workspace: header.cwd ?? workspaceRoot,
                            objective: summary.objective,
                            status: 'done',
                            resultText: summary.resultText,
                            toolCount: summary.toolCount,
                            messageCount: summary.messageCount,
                            createdAt: now,
                        }, {
                            eventId: digestEventIdForRound(principalId, header.id, count),
                            summary: summary.summary,
                        });
                        await hub.appendSharedEvent(digest);
                        probe(`appended ${header.id} round=${count}`);
                        state[header.id] = { count, lastPromptAt, digestAt: now };
                        saveState(statePath, state);
                        attempts.delete(header.id);
                        ctx.logger.info(`agent-society-session-digest wrote digest for ${header.id} (round ${count}, persisted path)`);
                    }
                    catch (error) {
                        attempts.set(header.id, (attempts.get(header.id) ?? 0) + 1);
                        ctx.logger.warn(`agent-society-session-digest failed for ${header.id}: ${error instanceof Error ? error.message : String(error)}`);
                    }
                    finally {
                        inFlight.delete(header.id);
                    }
                }
            }
            catch (error) {
                ctx.logger.warn(`agent-society-session-digest persistence scan failed: ${error instanceof Error ? error.message : String(error)}`);
            }
        }
    }
    async function handleRound(sessionId, events, options) {
        const now = Date.now();
        const count = events.length;
        const prior = state[sessionId];
        if (prior !== undefined && count <= prior.count)
            return;
        const lastEventTime = lastEventTimeMs(events);
        if (lastEventTime !== undefined && now - lastEventTime < idleSeconds * 1_000) {
            return;
        }
        if (inFlight.has(sessionId))
            return;
        if (attempts.get(sessionId) ?? 0 >= MAX_ATTEMPTS)
            return;
        inFlight.add(sessionId);
        try {
            const summary = await summarizeFor({ id: sessionId, events }, count, { live: options.live });
            const digest = buildSessionDigest({
                principalId,
                sessionId,
                actorId,
                nodeId,
                title: summary.title,
                workspace: workspaceRoot,
                objective: summary.objective,
                status: 'done',
                resultText: summary.resultText,
                toolCount: summary.toolCount,
                messageCount: summary.messageCount,
                createdAt: now,
            }, {
                eventId: digestEventIdForRound(principalId, sessionId, count),
                summary: summary.summary,
            });
            await hub.appendSharedEvent(digest);
            state[sessionId] = { count, digestAt: now };
            saveState(statePath, state);
            attempts.delete(sessionId);
            ctx.logger.info(`agent-society-session-digest wrote digest for ${sessionId} (round ${count})`);
        }
        catch (error) {
            attempts.set(sessionId, (attempts.get(sessionId) ?? 0) + 1);
            ctx.logger.warn(`agent-society-session-digest failed for ${sessionId}: ${error instanceof Error ? error.message : String(error)}`);
        }
        finally {
            inFlight.delete(sessionId);
        }
    }
    async function summarizeFor(session, count, options) {
        const title = options.title ?? sessionTitleOf(session);
        const fields = { ...extractFields(session), title };
        if (!summarize) {
            return { ...fields, summary: deterministicSummary(fields) };
        }
        try {
            const live = ctx.get('sessions');
            const liveSession = options.live ? live?.get(session.id) : undefined;
            if (liveSession !== undefined && assembly !== undefined) {
                const text = await summarizeLiveSession({
                    ctx,
                    session: liveSession,
                    assembly,
                    provider,
                    model,
                    maxTokens,
                });
                return { ...fields, summary: text };
            }
            const text = await summarizeStandalone({
                ctx,
                fields,
                provider,
                model,
                maxTokens,
            });
            return { ...fields, summary: text };
        }
        catch (error) {
            ctx.logger.warn(`agent-society-session-digest summary failed, using deterministic fallback: ${error instanceof Error ? error.message : String(error)}`);
            return { ...fields, summary: deterministicSummary(fields) };
        }
    }
    function sessionTitleOf(session) {
        const service = ctx.get('sessionTitle');
        return service?.get(session)?.title;
    }
}
function messageTextOf(event) {
    const data = event.data;
    const content = data?.message?.content;
    if (!Array.isArray(content))
        return '';
    return content
        .filter((block) => block !== null &&
        typeof block === 'object' &&
        block.type === 'text')
        .map((block) => block.text ?? '')
        .join('')
        .trim();
}
function lastEventTimeMs(events) {
    for (let index = events.length - 1; index >= 0; index -= 1) {
        const event = events[index];
        if (event && Number.isFinite(event.time))
            return event.time;
    }
    return undefined;
}
export function extractFields(session) {
    const events = session.events ?? [];
    let objective = '';
    let resultText = '';
    let toolCount = 0;
    let messageCount = 0;
    for (const event of events) {
        if (!event)
            continue;
        const type = event.type;
        if (type === 'user/message') {
            messageCount += 1;
            const text = messageTextOf(event);
            if (text && !objective)
                objective = text.slice(0, 200);
        }
        else if (type === 'assistant/message') {
            messageCount += 1;
            const text = messageTextOf(event);
            if (text)
                resultText = text.slice(0, 1_000);
        }
        else if (type === 'tool/call') {
            toolCount += 1;
        }
    }
    return {
        title: undefined,
        objective,
        resultText,
        toolCount,
        messageCount,
    };
}
export function digestEventIdForRound(principalId, sessionId, roundCount) {
    // Same round, same id — a restart or duplicate trigger cannot duplicate.
    return createHash('sha256')
        .update([principalId, 'consensus', 'digest', sessionId, String(roundCount)].join('\u0000'))
        .digest('hex');
}
function loadState(path) {
    try {
        const parsed = JSON.parse(readFileSync(path, 'utf8'));
        if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
            const record = parsed;
            const result = {};
            for (const [key, value] of Object.entries(record)) {
                if (value !== null &&
                    typeof value === 'object' &&
                    typeof value.count === 'number') {
                    const lastPromptAt = value.lastPromptAt;
                    result[key] = {
                        count: value.count,
                        ...(lastPromptAt === undefined ? {} : { lastPromptAt }),
                        digestAt: value.digestAt ?? 0,
                    };
                }
            }
            return result;
        }
    }
    catch {
        // Missing or partial state is a normal first run.
    }
    return {};
}
function saveState(path, state) {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    writeFileSync(path, `${JSON.stringify(state, null, 2)}\n`, {
        encoding: 'utf8',
        mode: 0o600,
    });
}
function stableSlug(value) {
    const slug = value.toLowerCase().replace(/[^a-z0-9._-]+/gu, '-');
    return slug.replace(/^-+|-+$/gu, '') || 'node';
}
//# sourceMappingURL=session-digest-watcher.js.map