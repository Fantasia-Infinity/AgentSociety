/**
 * AgentSociety question bridge for interactive dsh processes (TUI / Web).
 *
 * Runs whenever the bundle is loaded with Hub credentials, independent of
 * the worker flag. In auto mode (default) questions addressed to this
 * actor are answered with the bounded standalone answering session while no
 * human is present; when a human is present (or POLICY=ask) the questions
 * stay pending for the browser/TUI question card (P6a client plugin).
 *
 * The presence flag is a plain value: the browser client plugin reports UI
 * activity through the bridge's RPC surface; until a client connects, the
 * process counts as unattended (auto-answer) only when explicitly
 * configured, mirroring the worker behavior.
 */
import type { Context } from '@deepseek-ai/cordis';
import Schema from '@deepseek-ai/schemastery';
export declare const name = "agent-society-question-bridge";
export declare const inject: string[];
export type QuestionPolicy = 'auto' | 'ask' | 'standalone';
export interface Config {
    hubUrl?: string;
    hubTokenEnv?: string;
    principalId?: string;
    actorId?: string;
    nodeId?: string;
    workspaceRoot?: string;
    provider?: string;
    model?: string;
    maxTokens?: number;
    pollSeconds?: number;
    /** auto | ask | standalone (AGENT_SOCIETY_QUESTION_POLICY). */
    policy?: QuestionPolicy;
}
export declare const Config: Schema<Config>;
export declare function apply(ctx: Context, config: Config): void;
