/**
 * LLM summarization for interactive-session consensus digests.
 *
 * KV-cache rules (non-negotiable):
 * - The live-session mode reuses the target session's OWN derived messages
 *   and its last persisted request/header system prompt, tools, route, and
 *   adapter config. It appends ONLY the summarization instruction as a
 *   trailing user message, so the session's prefix is not borrowed from a
 *   different session or process-global assembly.
 * - The standalone mode never copies session context: a fixed system prompt
 *   plus one structured user payload. It is the fallback when no live session
 *   header is available.
 * - Both modes are bounded: default maxTokens 768, hard cap 1024, output clipped to
 *   {@link MAX_SUMMARY_CHARS}; failures fall back to the deterministic
 *   extractor so a consensus digest is still produced.
 */
import { createUserMessage } from '@deepseek-ai/dsh-llm';
export const MAX_SUMMARY_CHARS = 600;
export const DEFAULT_SUMMARY_MAX_TOKENS = 768;
export const SUMMARY_MAX_TOKENS_HARD_LIMIT = 1_024;
export const SUMMARY_TIMEOUT_MS = 30_000;
export const SUMMARIZE_PROMPT = ('Summarize this session in Chinese, at most 300 tokens: its goal, key ' +
    'actions, and the conclusion reached. Output only the summary text.').replace(/\s+/gu, ' ');
export const SUMMARIZE_SYSTEM = ('You are the AgentSociety consensus summarizer. Return only a concise, '
    + 'reusable Chinese summary of the supplied structured session notes.').replace(/\s+/gu, ' ');
/** Deterministic fallback: bounded field concatenation, no LLM involved. */
export function deterministicSummary(fields) {
    const parts = [
        fields.objective ? `目标：${fields.objective}` : '',
        fields.resultText ? `结果：${fields.resultText}` : '',
        fields.messageCount > 0 ? `消息 ${fields.messageCount} 条` : '',
        fields.toolCount > 0 ? `工具调用 ${fields.toolCount} 次` : '',
    ].filter(Boolean);
    const text = parts.join('；');
    return text.slice(0, MAX_SUMMARY_CHARS);
}
function boundedMaxTokens(value) {
    return Math.min(Math.max(1, Math.floor(Number.isFinite(value) ? value : DEFAULT_SUMMARY_MAX_TOKENS)), SUMMARY_MAX_TOKENS_HARD_LIMIT);
}
/** Live-session mode: target session header/history prefix + trailing instruction. */
export async function summarizeLiveSession(options) {
    const { ctx, session, requestHeader, maxTokens } = options;
    const llm = ctx.get('llm');
    if (llm === undefined || typeof llm.prepareCall !== 'function') {
        throw new Error('llm service unavailable');
    }
    const history = session.deriveMessages();
    const instruction = createUserMessage({
        content: [{ type: 'text', text: SUMMARIZE_PROMPT }],
        source: { kind: 'user' },
    });
    // Keep the target session's route and adapter-resolved controls. Only the
    // auxiliary output ceiling is intentionally smaller than the conversation
    // request; system/tools/history remain the exact session-owned prefix.
    const prepared = await llm.prepareCall({
        ...requestHeader.config,
        maxTokens: boundedMaxTokens(maxTokens),
    }, undefined);
    // The request's call-config fields must match the materialized prepared
    // config exactly (adapter defaults included), or the dispatch rejects
    // with "prepared LLM call config changed before adapter dispatch".
    // Since dsh 0.1.5 the system prompt is a durable `system/message` in the
    // session surface and therefore already appears in `history`. Keep reading
    // the retired field only for older callers/test doubles.
    const legacySystem = requestHeader.system;
    const stream = prepared.stream({
        ...prepared.config,
        messages: [...history, instruction],
        ...(typeof legacySystem === 'string' && legacySystem.length > 0
            ? { system: legacySystem }
            : {}),
        ...(requestHeader.tools && requestHeader.tools.length > 0
            ? { tools: requestHeader.tools }
            : {}),
    });
    return collectText(stream);
}
/** Standalone mode: fixed system prefix plus a structured dynamic user payload. */
export async function summarizeStandalone(options) {
    const { ctx, fields, provider, model, maxTokens } = options;
    const llm = ctx.get('llm');
    if (llm === undefined || typeof llm.prepareCall !== 'function') {
        throw new Error('llm service unavailable');
    }
    const prompt = JSON.stringify({
        title: fields.title?.slice(0, 200) ?? null,
        objective: fields.objective.slice(0, 500),
        result: fields.resultText.slice(0, 1_500),
        messageCount: fields.messageCount,
        toolCount: fields.toolCount,
    });
    const prepared = await llm.prepareCall({
        provider,
        model,
        maxTokens: boundedMaxTokens(maxTokens),
        reasoningEffort: 'off',
    }, undefined);
    const stream = prepared.stream({
        ...prepared.config,
        messages: [createUserMessage({
                content: [{ type: 'text', text: prompt }],
                source: { kind: 'user' },
            })],
        system: SUMMARIZE_SYSTEM,
    });
    return collectText(stream);
}
async function collectText(stream) {
    const deadline = Date.now() + SUMMARY_TIMEOUT_MS;
    let text = '';
    for await (const chunk of stream) {
        if (Date.now() > deadline)
            throw new Error('summary timed out');
        if (chunk.type === 'text-delta') {
            text += chunk.text;
        }
        else if (chunk.type === 'block-end' &&
            chunk.block !== null &&
            typeof chunk.block === 'object' &&
            chunk.block.type === 'text') {
            const blockText = chunk.block.text;
            if (typeof blockText === 'string')
                text += blockText;
        }
    }
    const trimmed = text.trim();
    if (!trimmed)
        throw new Error('summary produced no text');
    return trimmed.slice(0, MAX_SUMMARY_CHARS);
}
//# sourceMappingURL=summarizer.js.map