/**
 * LLM summarization for interactive-session consensus digests.
 *
 * KV-cache rules (non-negotiable):
 * - The live-session mode reuses the session's OWN derived messages
 *   (`session.deriveMessages()`, byte-identical to what dsh just sent the
 *   model) plus the cached assembly system/tools, and appends ONLY the
 *   summarization instruction as a trailing user message. The prefix
 *   (system + tools + full history) is unchanged, so provider prefix
 *   caches hit on the hot session prefix.
 * - The standalone mode never copies session context: a fixed template
 *   prefix plus extracted fields. It is the fallback when no live session
 *   (or no assembly cache) is available.
 * - Both modes are bounded: maxTokens 2048, output clipped to
 *   {@link MAX_SUMMARY_CHARS}; failures fall back to the deterministic
 *   extractor so a consensus digest is still produced.
 */
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { renderPrompt } from '@deepseek-ai/dsh-system-prompt';
export const MAX_SUMMARY_CHARS = 600;
export const SUMMARY_TIMEOUT_MS = 30_000;
export const SUMMARIZE_PROMPT = ('Summarize this session in Chinese, at most 300 tokens: its goal, key ' +
    'actions, and the conclusion reached. Output only the summary text.').replace(/\s+/gu, ' ');
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
/**
 * Live-session mode: session-derived prefix + trailing instruction.
 * The prefix is byte-identical to the session's own requests, so provider
 * prefix caches are reused; only the instruction + output are new tokens.
 */
export async function summarizeLiveSession(options) {
    const { ctx, session, assembly, provider, model, maxTokens } = options;
    const llm = ctx.get('llm');
    if (llm === undefined || typeof llm.prepareCall !== 'function') {
        throw new Error('llm service unavailable');
    }
    const history = session.deriveMessages();
    const instruction = createUserMessage({
        content: [{ type: 'text', text: SUMMARIZE_PROMPT }],
        source: { kind: 'user' },
    });
    const prepared = await llm.prepareCall({ provider, model, maxTokens }, undefined);
    // The request's call-config fields must match the materialized prepared
    // config exactly (adapter defaults included), or the dispatch rejects
    // with "prepared LLM call config changed before adapter dispatch".
    const stream = prepared.stream({
        ...prepared.config,
        provider,
        model,
        messages: [...history, instruction],
        system: renderPrompt(assembly),
        tools: assembly.tools,
    });
    return collectText(stream);
}
/**
 * Standalone mode: fixed template + extracted fields, no session context.
 * Used when the session is not live in this process (or the assembly
 * snapshot is missing). The fixed instruction prefix is itself cacheable.
 */
export async function summarizeStandalone(options) {
    const { ctx, fields, provider, model, maxTokens } = options;
    const llm = ctx.get('llm');
    if (llm === undefined || typeof llm.prepareCall !== 'function') {
        throw new Error('llm service unavailable');
    }
    const prompt = ('Summarize the following session notes in Chinese, at most 300 tokens: ' +
        'goal, key actions, conclusion. Output only the summary text.\n\n' +
        `Title: ${fields.title ?? '-'}\n` +
        `Objective: ${fields.objective.slice(0, 500) || '-'}\n` +
        `Result: ${fields.resultText.slice(0, 1_500) || '-'}\n` +
        `Messages: ${fields.messageCount}; Tools: ${fields.toolCount}`).replace(/\s+/gu, ' ');
    const prepared = await llm.prepareCall({ provider, model, maxTokens, reasoningEffort: 'off' }, undefined);
    const stream = prepared.stream({
        ...prepared.config,
        provider,
        model,
        messages: [createUserMessage({ content: [{ type: 'text', text: prompt }], source: { kind: 'user' } })],
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