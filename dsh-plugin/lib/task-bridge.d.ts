/**
 * `agent-society-task-bridge`: let the interactive UI session of this dsh
 * process (web) accept and execute Hub tasks inside its own conversation.
 *
 * The web process polls for tasks addressed to this actor. A task is only
 * claimed while the UI session's agent is idle (no turn in flight), and the
 * whole execution lands in the session history via `followup(TASK_PROMPT)` —
 * the human sees the run when they return, and the result is written back to
 * the Hub task plus a structured task digest into shared memory. A human
 * talking to the session keeps the agent busy, so the bridge simply waits for
 * the next idle round; tasks addressed to this actor are never executed in
 * worker sessions (those belong to the worker plugin).
 *
 * Enabled for the web surface via AGENT_SOCIETY_UI_TASKS=1.
 */
import type { Context } from '@deepseek-ai/cordis';
import Schema from '@deepseek-ai/schemastery';
export declare const name = "agent-society-task-bridge";
export declare const inject: string[];
export interface Config {
    hubUrl?: string;
    hubTokenEnv?: string;
    principalId?: string;
    actorId?: string;
    nodeId?: string;
    workspaceRoot?: string;
    pollSeconds?: number;
}
export declare const Config: Schema<Config>;
export declare function apply(ctx: Context, config: Config): void;
