/**
 * AgentSociety directory sync plugin.
 *
 * Runs inside every hub-connected dsh process (TUI / Web / worker). It
 * (a) pushes depth-0/1 rows for the local sessions visible through dsh
 * session persistence, and (b) maintains the local mirror of the Hub's
 * per-principal directory (`~/.dsh/agent-society-directory.json`) via
 * incremental pulls. Staleness is bounded by `pullSeconds` (default 10s);
 * the worker plugin additionally pushes invocation rows on task run end.
 */
import type { Context } from '@deepseek-ai/cordis';
import Schema from '@deepseek-ai/schemastery';
export declare const name = "agent-society-directory";
export declare const inject: string[];
export interface Config {
    hubUrl?: string;
    hubTokenEnv?: string;
    principalId?: string;
    actorId?: string;
    nodeId?: string;
    workspaceRoot?: string;
    sessionMode?: string;
    toolPolicy?: string;
    pullSeconds?: number;
}
export declare const Config: Schema<Config>;
export declare function apply(ctx: Context, config: Config): void;
