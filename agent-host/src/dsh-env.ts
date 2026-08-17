/**
 * Environment assembly for dsh child launchers.
 *
 * TUI, Web, worker, and dispatch all share a small set of model / Hub /
 * session variables. Keeping the assembly here (instead of inside cli.ts)
 * makes each launcher only responsible for its command line and result
 * handling.
 */

import { sanitizedChildEnv } from "./child-env.js";
import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";

/** Whether the DeepSeek web search provider should be enabled for dsh children. */
export function pluginWebSearchEnabled(config: AgentHostConfig): boolean {
  if (config.dshWebSearch === false || config.webSearchMode === "disabled") {
    return false;
  }
  if (config.webSearchMode === "deepseek") return true;
  try {
    return (
      new URL(config.remoteBaseUrl ?? "").hostname.toLowerCase() ===
      "api.deepseek.com"
    );
  } catch {
    return false;
  }
}

/**
 * Environment shared by every dsh surface (TUI / Web / worker).
 */
export function buildDshCommonEnv(
  config: AgentHostConfig,
  hub: HubClient | undefined,
  options: {
    worker: boolean;
    hubMcp: boolean;
    hubConnection?: boolean;
  },
): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {
    ...sanitizedChildEnv(process.env),
    AGENT_SOCIETY_WORKER: options.worker ? "1" : "0",
    AGENT_SOCIETY_WEB_SEARCH: pluginWebSearchEnabled(config) ? "1" : "0",
    AGENT_SOCIETY_SESSION_COMPRESSION: config.dshSessionCompression,
    AGENT_SOCIETY_ACTOR_ID: config.actorId,
    AGENT_SOCIETY_NODE_ID: config.nodeId,
    AGENT_SOCIETY_PRINCIPAL_ID: config.principalId,
    AGENT_SOCIETY_DISPLAY_NAME: config.actorDisplayName,
  };
  if (hub && config.hubUrl) {
    if (options.hubConnection) {
      env.AGENT_SOCIETY_HUB_URL = config.hubUrl;
      env.AGENT_SOCIETY_HUB_TOKEN = config.hubToken ?? hub.nodeToken;
    }
    env.AGENT_SOCIETY_HUB_MCP = options.hubMcp ? "1" : "0";
    if (options.hubMcp) {
      env.AGENT_SOCIETY_HUB_URL = config.hubUrl;
      env.AGENT_SOCIETY_HUB_TOKEN = config.hubToken ?? hub.nodeToken;
    }
  } else {
    env.AGENT_SOCIETY_HUB_MCP = "0";
  }
  if (config.remoteApiKey) env.DEEPSEEK_API_KEY = config.remoteApiKey;
  if (config.remoteBaseUrl) env.DEEPSEEK_BASE_URL = config.remoteBaseUrl;
  return env;
}

/** Environment for the in-process dsh plugin worker. */
export function buildDshWorkerEnv(
  config: AgentHostConfig,
  hub: HubClient,
  repositoryRoot: string,
): NodeJS.ProcessEnv {
  const model = config.dshModel ?? config.remoteModel ?? "deepseek-v4-flash";
  return {
    ...buildDshCommonEnv(config, hub, {
      worker: true,
      hubMcp: config.dshHubMcp !== false,
      hubConnection: true,
    }),
    AGENT_SOCIETY_WORKSPACE_ROOT: config.workspaceRoot,
    AGENT_SOCIETY_SESSION_MODE: config.workerSessionMode,
    AGENT_SOCIETY_TOOL_POLICY: config.remoteToolPolicy,
    AGENT_SOCIETY_REPOSITORY_ROOT: repositoryRoot,
    AGENT_SELF_UPDATE: config.selfUpdateEnabled === false ? "0" : "1",
    AGENT_SOCIETY_POLL_SECONDS: String(config.pollSeconds),
    AGENT_SOCIETY_LEASE_SECONDS: String(config.leaseSeconds),
    AGENT_SOCIETY_ACTOR_ID: config.actorId,
    AGENT_SOCIETY_NODE_ID: config.nodeId,
    AGENT_SOCIETY_PRINCIPAL_ID: config.principalId,
    AGENT_SOCIETY_DISPLAY_NAME: config.actorDisplayName,
    AGENT_SOCIETY_PROVIDER: config.dshProvider ?? "deepseek-official",
    AGENT_SOCIETY_MODEL: model,
    DSH_MODEL: model,
    AGENT_SOCIETY_MAX_TOKENS: String(
      config.dshMaxTokens ?? config.maxOutputTokens,
    ),
    AGENT_SOCIETY_SESSION_COMPRESSION:
      config.dshSessionCompression ?? "none",
  };
}

/** Environment for the legacy `dsh-dispatch` web launcher. */
export function buildDshDispatchEnv(
  config: AgentHostConfig,
  hub: HubClient,
): NodeJS.ProcessEnv {
  return {
    ...sanitizedChildEnv(process.env),
    AGENT_SOCIETY_HUB_URL: config.hubUrl!,
    AGENT_SOCIETY_HUB_MCP_TOKEN: hub.nodeToken,
  };
}
