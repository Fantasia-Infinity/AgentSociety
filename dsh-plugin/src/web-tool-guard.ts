/**
 * Keep the DeepSeek web_search tool visible in every agent assembly.
 *
 * Presets are allowed to filter the assembled tool catalog. Web search is
 * a deployment-level provider capability (like the Hub MCP surface), so this
 * guard re-appends it after the ordinary assembly waterfall has run. It reads
 * the scoped tool registry AFTER restrictions, so an explicit
 * `tools.restrict()` denial is still honored.
 */

import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-system-prompt'
import type { ToolSchema } from '@deepseek-ai/dsh-llm'

export const name = 'agent-society-web-tool-guard'
export const inject: string[] = []

const WEB_TOOL_NAME = 'web_search'

interface ToolRuntimeLike {
  schemas(scope?: object): readonly ToolSchema[]
}

export function apply(ctx: Context): void {
  const disabled = process.env.AGENT_SOCIETY_WEB_SEARCH === "0";
  ctx.on(
    'system-prompt/assemble',
    async (assembly, context, next) => {
      const assembled = await next()
      try {
        if (disabled) {
          return {
            ...assembled,
            tools: assembled.tools.filter((tool) => tool.name !== WEB_TOOL_NAME),
          }
        }
        const tools = ctx.get('tools') as ToolRuntimeLike | undefined
        const webTool = (tools?.schemas(context.scope) ?? []).find(
          (tool) => tool.name === WEB_TOOL_NAME,
        )
        if (!webTool) return assembled
        if (assembled.tools.some((tool) => tool.name === WEB_TOOL_NAME)) {
          return assembled
        }
        return {
          ...assembled,
          tools: [...assembled.tools, webTool],
        }
      } catch (error) {
        ctx.logger.warn(
          `agent-society-web-tool-guard failed; keeping the assembled catalog: ${error instanceof Error ? error.message : String(error)}`,
        )
        return assembled
      }
    },
    { prepend: true },
  )
}
