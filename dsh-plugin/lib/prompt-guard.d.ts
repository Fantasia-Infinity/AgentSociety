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
import type { Context } from '@deepseek-ai/cordis';
declare module '@deepseek-ai/dsh-llm' {
    interface MessageSourceMap {
        'agent-society-directory-index': {
            kind: 'agent-society-directory-index';
        };
    }
}
export declare const name = "agent-society-directory-index";
export declare const inject: string[];
export type SharedContextMode = 'balanced' | 'legacy' | 'off';
export declare function sharedContextMode(env?: NodeJS.ProcessEnv): SharedContextMode;
export declare function apply(ctx: Context): void;
