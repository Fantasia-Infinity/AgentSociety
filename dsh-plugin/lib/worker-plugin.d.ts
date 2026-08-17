/**
 * AgentSociety worker plugin for DeepSeek Harness.
 *
 * In-process execution path: claims tasks from the AgentSociety Hub, drives
 * dsh agents through `ctx.agents.create()` / `ctx.agents.resume()`, applies
 * per-task tool policies, attaches durable transcripts as Hub artifacts, and
 * writes task results back to the Hub.
 */
import type { Context } from '@deepseek-ai/cordis';
import Schema from '@deepseek-ai/schemastery';
export declare const name = "agent-society-worker";
export declare const inject: string[];
export type ToolPolicy = 'full' | 'read_only' | 'no_tools';
export interface Config {
    hubUrl?: string;
    hubTokenEnv?: string;
    pollSeconds?: number;
    leaseSeconds?: number;
    actorId?: string;
    nodeId?: string;
    principalId?: string;
    displayName?: string;
    workspaceRoot?: string;
    sessionMode?: 'per_task' | 'continuous';
    toolPolicy?: ToolPolicy;
    selfUpdateEnabled?: boolean;
    repositoryRoot?: string;
    provider?: string;
    model?: string;
    maxTokens?: number;
    /** Append consensus digests to the Hub shared memory (AGENT_SOCIETY_CONTEXT). */
    contextEnabled?: boolean;
    /** Push session directory rows / invocations (default on). */
    directoryEnabled?: boolean;
}
export declare const Config: Schema<Config>;
export declare function apply(ctx: Context, config: Config): Promise<void>;
