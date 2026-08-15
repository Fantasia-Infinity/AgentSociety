/**
 * AgentSociety worker plugin for DeepSeek Harness.
 *
 * This is the first in-process dsh execution path: it claims tasks from the
 * AgentSociety Hub, drives dsh agents through `ctx.agents.create()` /
 * `ctx.agents.resume()`, and writes task results back to the Hub.
 */
import type { Context } from '@deepseek-ai/cordis';
import Schema from '@deepseek-ai/schemastery';
export declare const name = "agent-society-worker";
export declare const inject: string[];
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
    provider?: string;
    model?: string;
    maxTokens?: number;
}
export declare const Config: Schema<Config>;
export declare function apply(ctx: Context, config: Config): Promise<void>;
