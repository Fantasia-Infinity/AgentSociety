/**
 * Self-update support for the in-process dsh worker plugin.
 *
 * A Hub task with `input.action === "self_update"` is handled by the worker
 * process itself, exactly like the Pi worker path: it pulls the AgentSociety
 * repository, installs changed dependencies, rebuilds both runtimes, reports
 * the result to the Hub, and exits with a dedicated code that makes the CLI
 * parent restart the dsh worker.
 */
import type { HubTask } from './hub-client.js';
export declare const SELF_UPDATE_ACTION = "self_update";
export declare const SELF_UPDATE_EXIT_CODE = 75;
export interface PluginSelfUpdateReport {
    ok: boolean;
    updated: boolean;
    needsRestart: boolean;
    steps: string[];
    error?: string;
    before?: string;
    after?: string;
}
export interface PluginSelfUpdateOptions {
    repositoryRoot: string;
    enabled: boolean;
    nodePath: string;
}
export declare function isSelfUpdateTask(task: HubTask): boolean;
export declare function runPluginSelfUpdate(task: HubTask, options: PluginSelfUpdateOptions): Promise<PluginSelfUpdateReport>;
