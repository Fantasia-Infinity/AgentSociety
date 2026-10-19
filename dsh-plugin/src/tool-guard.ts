/**
 * Shared tool-guard factory for AgentSociety's assembly guards.
 *
 * Both the Hub MCP guard and the web search guard follow the same shape:
 * run after the ordinary system-prompt assembly, inspect the scoped tool
 * registry, and re-insert (or remove) deployment-level tools without
 * overriding a real `tools.restrict()` denial.
 */

import type { Context } from '@deepseek-ai/cordis'
import type { PromptAssembly } from '@deepseek-ai/dsh-system-prompt'
import type { ToolSchema } from '@deepseek-ai/dsh-llm'

export interface ToolRuntimeLike {
  schemas(scope?: object): readonly ToolSchema[]
}

type Assembly = PromptAssembly

/**
 * Optional pre-step and post-step transforms for one guard.
 */
export interface ToolGuardDefinition {
  /** Guard plugin name used in diagnostics. */
  readonly name: string
  /** When `false`, the guard leaves the assembly untouched. */
  readonly enabled?: () => boolean
  /** Collect the tools this guard manages from the scoped registry. */
  readonly collect: (
    tools: ToolRuntimeLike,
    scope: object | undefined,
  ) => readonly ToolSchema[] | Promise<readonly ToolSchema[]>
  /** Optional transform before the collected tools are considered. */
  readonly before?: (
    assembly: Assembly,
    tools: ToolRuntimeLike,
    scope: object | undefined,
  ) => Assembly | undefined
  /** Optional transform after collection (default: append missing tools). */
  readonly after?: (
    assembly: Assembly,
    found: readonly ToolSchema[],
  ) => Assembly | undefined
}

/**
 * Register one system-prompt assembly guard. `after` defaults to appending
 * any collected tool that is not already present in the assembly.
 */
export function createToolGuard(ctx: Context, definition: ToolGuardDefinition): void {
  const enabled = definition.enabled ?? ((): boolean => true)
  ctx.on(
    'system-prompt/assemble',
    async (assembly, context, next) => {
      const assembled = await next()
      if (!enabled()) return assembled
      try {
        const tools = ctx.get('tools') as ToolRuntimeLike | undefined
        const scope = context.scope as object | undefined
        let current = assembled
        if (definition.before !== undefined) {
          const transformed = definition.before(assembled, tools as ToolRuntimeLike, scope)
          if (transformed !== undefined) current = transformed
        }
        const found = tools === undefined
          ? []
          : await definition.collect(tools, scope)
        const after = definition.after ?? defaultAppend
        const result = after(current, stableToolSchemas(found))
        if (result === undefined) return assembled
        return result
      } catch (error) {
        ctx.logger.warn(
          `${definition.name} failed; keeping the assembled catalog: ${error instanceof Error ? error.message : String(error)}`,
        )
        return assembled
      }
    },
    { prepend: true },
  )
}

export function defaultAppend(
  assembly: Assembly,
  found: readonly ToolSchema[],
): Assembly | undefined {
  if (found.length === 0) return undefined
  const existing = new Set(assembly.tools.map((tool) => tool.name))
  const missing = [...found]
    .filter((tool) => !existing.has(tool.name))
    .sort((a, b) => stringCompare(a.name, b.name))
  if (missing.length === 0) return undefined
  return {
    ...assembly,
    tools: [...assembly.tools, ...missing],
  }
}

/** Recursively canonicalize JSON-schema object keys before they enter a
 * request header. Arrays retain their declared order because `required` and
 * enum arrays can be semantically ordered by an adapter. */
export function stableToolSchemas(
  tools: readonly ToolSchema[],
): ToolSchema[] {
  return tools.map((tool) => stableValue(tool) as ToolSchema)
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([a], [b]) => stringCompare(a, b))
        .map(([key, child]) => [key, stableValue(child)]),
    )
  }
  return value
}

function stringCompare(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}
