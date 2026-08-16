/**
 * Shared tool-guard factory for AgentSociety's assembly guards.
 *
 * Both the Hub MCP guard and the web search guard follow the same shape:
 * run after the ordinary system-prompt assembly, inspect the scoped tool
 * registry, and re-insert (or remove) deployment-level tools without
 * overriding a real `tools.restrict()` denial.
 */
import type { Context } from '@deepseek-ai/cordis';
import type { PromptAssembly } from '@deepseek-ai/dsh-system-prompt';
import type { ToolSchema } from '@deepseek-ai/dsh-llm';
export interface ToolRuntimeLike {
    schemas(scope?: object): readonly ToolSchema[];
}
type Assembly = PromptAssembly;
/**
 * Optional pre-step and post-step transforms for one guard.
 */
export interface ToolGuardDefinition {
    /** Guard plugin name used in diagnostics. */
    readonly name: string;
    /** When `false`, the guard leaves the assembly untouched. */
    readonly enabled?: () => boolean;
    /** Collect the tools this guard manages from the scoped registry. */
    readonly collect: (tools: ToolRuntimeLike, scope: object | undefined) => readonly ToolSchema[] | Promise<readonly ToolSchema[]>;
    /** Optional transform before the collected tools are considered. */
    readonly before?: (assembly: Assembly, tools: ToolRuntimeLike, scope: object | undefined) => Assembly | undefined;
    /** Optional transform after collection (default: append missing tools). */
    readonly after?: (assembly: Assembly, found: readonly ToolSchema[]) => Assembly | undefined;
}
/**
 * Register one system-prompt assembly guard. `after` defaults to appending
 * any collected tool that is not already present in the assembly.
 */
export declare function createToolGuard(ctx: Context, definition: ToolGuardDefinition): void;
export {};
