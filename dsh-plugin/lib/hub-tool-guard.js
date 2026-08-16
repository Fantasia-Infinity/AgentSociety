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
export const name = 'agent-society-hub-tool-guard';
export const inject = [];
const HUB_TOOL_PREFIX = 'mcp__agent-society__hub_';
const HUB_TOOL_READY_TIMEOUT_MS = 10_000;
let waitedForHubTools = false;
export function apply(ctx) {
    ctx.on('system-prompt/assemble', async (assembly, context, next) => {
        const assembled = await next();
        try {
            const tools = ctx.get('tools');
            if (!tools)
                return assembled;
            let visible = hubToolsIn(tools, context.scope);
            if (visible.length === 0 && !waitedForHubTools) {
                waitedForHubTools = true;
                visible = await waitForHubTools(tools, context.scope);
            }
            if (visible.length === 0)
                return assembled;
            const existing = new Set(assembled.tools.map((tool) => tool.name));
            const missing = visible.filter((tool) => !existing.has(tool.name));
            if (missing.length === 0)
                return assembled;
            return {
                ...assembled,
                tools: [...assembled.tools, ...missing],
            };
        }
        catch (error) {
            ctx.logger.warn(`agent-society-hub-tool-guard failed; keeping the assembled catalog: ${error instanceof Error ? error.message : String(error)}`);
            return assembled;
        }
    }, { prepend: true });
}
function hubToolsIn(tools, scope) {
    return (tools.schemas(scope) ?? []).filter((tool) => tool.name.startsWith(HUB_TOOL_PREFIX));
}
async function waitForHubTools(tools, scope) {
    const deadline = Date.now() + HUB_TOOL_READY_TIMEOUT_MS;
    for (;;) {
        const visible = hubToolsIn(tools, scope);
        if (visible.length > 0)
            return visible;
        if (Date.now() >= deadline)
            return [];
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
}
//# sourceMappingURL=hub-tool-guard.js.map