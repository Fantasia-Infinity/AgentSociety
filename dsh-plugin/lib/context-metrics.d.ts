/**
 * Prompt/cache diagnostics that never persist prompt text, summaries, or
 * credentials. The file is deliberately best-effort: an unwritable metrics
 * path must not affect an interactive request or a digest.
 */
export type ContextMetricKind = 'initial' | 'delta' | 'snapshot';
export interface AgentSocietyMetrics {
    readonly version: 1;
    readonly updatedAt: number;
    readonly cacheReadTokens: number;
    readonly inputTokens: number;
    readonly uncachedInputTokens: number;
    readonly contextInitialBytes: number;
    readonly contextDeltaBytes: number;
    readonly contextSnapshotBytes: number;
    readonly contextUpdatesPerSession: Readonly<Record<string, {
        readonly initial: number;
        readonly delta: number;
        readonly snapshot: number;
        readonly lastBytes: number;
    }>>;
    readonly digestModelCalls: number;
    readonly digestDeduplicatedCalls: number;
    readonly hubToolSchemaHash?: string;
}
export declare function contextMetricsPath(dshHome: string): string;
export declare function recordAgentSocietyMetrics(dshHome: string, update: {
    readonly context?: {
        readonly kind: ContextMetricKind;
        readonly bytes: number;
        readonly sessionId?: string;
    };
    readonly digestModelCall?: boolean;
    readonly digestDeduplicatedCall?: boolean;
    readonly hubToolSchemaHash?: string;
    readonly usage?: {
        readonly inputTokens: number;
        readonly cacheReadTokens?: number;
        readonly cacheWriteTokens?: number;
    };
}): void;
export declare function hashToolSchemas(tools: readonly unknown[]): string;
