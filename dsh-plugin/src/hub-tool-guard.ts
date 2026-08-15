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

import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-system-prompt'
import type { ToolSchema } from '@deepseek-ai/dsh-llm'

export const name = 'agent-society-hub-tool-guard'
export const inject: string[] = []

const HUB_TOOL_PREFIX = 'mcp__agent-society__hub_'

interface ToolRuntimeLike {
  schemas(scope?: object): readonly ToolSchema[]
}

export function apply(ctx: Context): void {
  ctx.on(
    'system-prompt/assemble',
    async (assembly, context, next) => {
      const assembled = await next()
      try {
        const tools = ctx.get('tools') as ToolRuntimeLike | undefined
        const visible = tools?.schemas(context.scope) ?? []
        const hubTools = visible.filter((tool) =>
          tool.name.startsWith(HUB_TOOL_PREFIX),
        )
        if (hubTools.length === 0) return assembled
        const existing = new Set(assembled.tools.map((tool) => tool.name))
        const missing = hubTools.filter(
          (tool) => !existing.has(tool.name),
        )
        if (missing.length === 0) return assembled
        return {
          ...assembled,
          tools: [...assembled.tools, ...missing],
        }
      } catch (error) {
        ctx.logger.warn(
          `agent-society-hub-tool-guard failed; keeping the assembled catalog: ${error instanceof Error ? error.message : String(error)}`,
        )
        return assembled
      }
    },
    { prepend: true },
  )
}
