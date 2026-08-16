/**
 * Shared tool-guard factory for AgentSociety's assembly guards.
 *
 * Both the Hub MCP guard and the web search guard follow the same shape:
 * run after the ordinary system-prompt assembly, inspect the scoped tool
 * registry, and re-insert (or remove) deployment-level tools without
 * overriding a real `tools.restrict()` denial.
 */
/**
 * Register one system-prompt assembly guard. `after` defaults to appending
 * any collected tool that is not already present in the assembly.
 */
export function createToolGuard(ctx, definition) {
    const enabled = definition.enabled ?? (() => true);
    ctx.on('system-prompt/assemble', async (assembly, context, next) => {
        const assembled = await next();
        if (!enabled())
            return assembled;
        try {
            const tools = ctx.get('tools');
            const scope = context.scope;
            let current = assembled;
            if (definition.before !== undefined) {
                const transformed = definition.before(assembled, tools, scope);
                if (transformed !== undefined)
                    current = transformed;
            }
            const found = tools === undefined
                ? []
                : await definition.collect(tools, scope);
            const after = definition.after ?? defaultAppend;
            const result = after(current, found);
            if (result === undefined)
                return assembled;
            return result;
        }
        catch (error) {
            ctx.logger.warn(`${definition.name} failed; keeping the assembled catalog: ${error instanceof Error ? error.message : String(error)}`);
            return assembled;
        }
    }, { prepend: true });
}
function defaultAppend(assembly, found) {
    if (found.length === 0)
        return undefined;
    const existing = new Set(assembly.tools.map((tool) => tool.name));
    const missing = found.filter((tool) => !existing.has(tool.name));
    if (missing.length === 0)
        return undefined;
    return {
        ...assembly,
        tools: [...assembly.tools, ...missing],
    };
}
//# sourceMappingURL=tool-guard.js.map