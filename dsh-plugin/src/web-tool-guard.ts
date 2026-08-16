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
import type { ToolSchema } from '@deepseek-ai/dsh-llm'
import { createToolGuard, type ToolRuntimeLike } from './tool-guard.js'

export const name = 'agent-society-web-tool-guard'
export const inject: string[] = []

const WEB_TOOL_NAME = 'web_search'

export function apply(ctx: Context): void {
  const disabled = process.env.AGENT_SOCIETY_WEB_SEARCH === '0'
  createToolGuard(ctx, {
    name,
    before: (assembly) => {
      if (!disabled) return undefined
      return {
        ...assembly,
        tools: assembly.tools.filter((tool) => tool.name !== WEB_TOOL_NAME),
      }
    },
    collect: (tools: ToolRuntimeLike, scope?: object) => {
      const webTool = (tools?.schemas(scope) ?? []).find(
        (tool) => tool.name === WEB_TOOL_NAME,
      )
      return webTool === undefined ? [] : [webTool]
    },
  })
}

export type { ToolSchema, ToolRuntimeLike }
