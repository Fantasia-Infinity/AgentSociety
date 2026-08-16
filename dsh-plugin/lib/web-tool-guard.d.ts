/**
 * Keep the DeepSeek web_search tool visible in every agent assembly.
 *
 * Presets are allowed to filter the assembled tool catalog. Web search is
 * a deployment-level provider capability (like the Hub MCP surface), so this
 * guard re-appends it after the ordinary assembly waterfall has run. It reads
 * the scoped tool registry AFTER restrictions, so an explicit
 * `tools.restrict()` denial is still honored.
 */
import type { Context } from '@deepseek-ai/cordis';
import type { ToolSchema } from '@deepseek-ai/dsh-llm';
import { type ToolRuntimeLike } from './tool-guard.js';
export declare const name = "agent-society-web-tool-guard";
export declare const inject: string[];
export declare function apply(ctx: Context): void;
export type { ToolSchema, ToolRuntimeLike };
