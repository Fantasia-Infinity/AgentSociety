/**
 * Keep the DeepSeek web_search tool visible in every agent assembly.
 *
 * Presets are allowed to filter the assembled tool catalog. Web search is
 * a deployment-level provider capability (like the Hub MCP surface), so this
 * guard re-appends it after the ordinary assembly waterfall has run. It reads
 * the scoped tool registry AFTER restrictions, so an explicit
 * `tools.restrict()` denial is still honored.
 */
import { createToolGuard } from './tool-guard.js';
export const name = 'agent-society-web-tool-guard';
export const inject = [];
const WEB_TOOL_NAME = 'web_search';
export function apply(ctx) {
    const disabled = process.env.AGENT_SOCIETY_WEB_SEARCH === '0';
    createToolGuard(ctx, {
        name,
        before: (assembly) => {
            if (!disabled)
                return undefined;
            return {
                ...assembly,
                tools: assembly.tools.filter((tool) => tool.name !== WEB_TOOL_NAME),
            };
        },
        collect: (tools, scope) => {
            const webTool = (tools?.schemas(scope) ?? []).find((tool) => tool.name === WEB_TOOL_NAME);
            return webTool === undefined ? [] : [webTool];
        },
    });
}
//# sourceMappingURL=web-tool-guard.js.map