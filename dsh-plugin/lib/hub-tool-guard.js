/**
 * Keep AgentSociety Hub MCP tools visible in every agent assembly.
 *
 * Agent presets are allowed to filter the assembled tool catalog (for
 * example the anchored-standard preset starts with a deliberately small
 * bootstrap catalog). Hub coordination tools are deployment-level dispatch
 * surface rather than preset capabilities, so this guard re-appends them
 * after the ordinary assembly waterfall has run. It reads the scoped tool
 * registry AFTER restrictions, so a real `tools.restrict()` denial is still
 * honored.
 */
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { hashToolSchemas, recordAgentSocietyMetrics } from './context-metrics.js';
import { createToolGuard, defaultAppend, stableToolSchemas, } from './tool-guard.js';
export const name = 'agent-society-hub-tool-guard';
export const inject = [];
const HUB_TOOL_PREFIX = 'mcp__agent-society__hub_';
const HUB_TOOL_READY_TIMEOUT_MS = 10_000;
let waitedForHubTools = false;
export function apply(ctx) {
    const dshHome = resolve(process.env.DSH_HOME?.trim() || join(homedir(), '.dsh'));
    let lastSchemaHash;
    createToolGuard(ctx, {
        name,
        after: (assembly, found) => {
            const appended = defaultAppend(assembly, found);
            const current = appended ?? assembly;
            const hubTools = stableToolSchemas(current.tools
                .filter((tool) => tool.name.startsWith(HUB_TOOL_PREFIX))
                .sort((a, b) => stringCompare(a.name, b.name)));
            const schemaHash = hashToolSchemas(hubTools);
            if (schemaHash !== lastSchemaHash) {
                lastSchemaHash = schemaHash;
                recordAgentSocietyMetrics(dshHome, { hubToolSchemaHash: schemaHash });
            }
            if (hubTools.length === 0)
                return appended;
            let position = 0;
            const tools = current.tools.map((tool) => tool.name.startsWith(HUB_TOOL_PREFIX)
                ? hubTools[position++]
                : tool);
            const sameOrder = tools.every((tool, index) => tool === current.tools[index]);
            return sameOrder ? appended : { ...current, tools };
        },
        collect: async (tools, scope) => {
            let visible = hubToolsIn(tools, scope);
            if (visible.length === 0 && !waitedForHubTools) {
                waitedForHubTools = true;
                visible = await waitForHubTools(tools, scope);
            }
            return visible;
        },
    });
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
function stringCompare(a, b) {
    return a < b ? -1 : a > b ? 1 : 0;
}
//# sourceMappingURL=hub-tool-guard.js.map