/**
 * Keep AgentSociety Hub MCP tools visible in every agent assembly.
 *
 * Agent presets are allowed to filter the assembled tool catalog (for
 * example the anchored-standard preset starts with a deliberately small
 * bootstrap catalog). Hub coordination tools are deployment-level dispatch
 * surface rather than preset capabilities, so this plugin re-appends them
 * after the ordinary assembly waterfall has run. It reads the scoped tool
 * registry AFTER restrictions, so a real `tools.restrict()` denial is still
 * honored.
 */
import type { Context } from '@deepseek-ai/cordis';
export declare const name = "agent-society-hub-tool-guard";
export declare const inject: string[];
export declare function apply(ctx: Context): void;
