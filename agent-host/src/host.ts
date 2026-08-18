import { hostname, platform, release } from "node:os";

import type { AgentHostConfig } from "./config.js";
import type { AdapterManifest } from "./bridge-types.js";
import type { HubClient } from "./hub-client.js";

export function nodeWebMetadata(
  config: AgentHostConfig,
): Record<string, unknown> | undefined {
  if (!config.dshWebEnabled) return undefined;
  return {
    enabled: true,
    protocol_version: "1",
    profile: config.dshWebProfile ?? "agent-society-web",
    capabilities: ["session.read"],
  };
}

export function nodeWebCapabilities(config: AgentHostConfig): string[] {
  return config.dshWebEnabled ? ["dsh-web"] : [];
}

export async function registerHost(
  config: AgentHostConfig,
  hub: HubClient,
): Promise<void> {
  await hub.registerPrincipal({
    principal_id: config.principalId,
    kind: "human",
    display_name: config.principalDisplayName,
    metadata: {},
  });
  await hub.registerActor({
    actor_id: config.actorId,
    principal_id: config.principalId,
    kind: "agent",
    display_name: config.actorDisplayName,
    capabilities: [
      "pi",
      "code",
      "hub-task",
      ...(config.builtinCapabilitiesEnabled
        ? [
            "subagent",
            "plan-todo",
            "long-term-memory",
            "lsp",
            "mcp",
            "background-process",
          ]
        : []),
      ...(config.webSearchMode !== "disabled" ? ["web-search"] : []),
      ...(config.remoteToolPolicy === "full" ? ["workspace-write"] : []),
    ],
    metadata: {
      runtime: "pi",
      runtime_version: "0.83.0",
      remote_tool_policy: config.remoteToolPolicy,
      builtin_capabilities: config.builtinCapabilitiesEnabled,
      worker_session_mode: config.workerSessionMode,
    },
  });
  await hub.registerNode({
    node_id: config.nodeId,
    actor_id: config.actorId,
    display_name: config.nodeDisplayName,
    capabilities: [
      "filesystem",
      "local-interactive",
      "remote-worker",
      ...nodeWebCapabilities(config),
    ],
    metadata: {
      platform: platform(),
      release: release(),
      workspace_root: config.workspaceRoot,
      ...(nodeWebMetadata(config)
        ? { dsh_web: nodeWebMetadata(config) }
        : {}),
    },
  });
}

export async function registerDshHost(
  config: AgentHostConfig,
  hub: HubClient,
): Promise<void> {
  await hub.registerPrincipal({
    principal_id: config.principalId,
    kind: "human",
    display_name: config.principalDisplayName,
    metadata: {},
  });
  await hub.registerActor({
    actor_id: config.actorId,
    principal_id: config.principalId,
    kind: "agent",
    display_name: config.actorDisplayName,
    capabilities: [
      "dsh",
      "code",
      "hub-task",
      ...(config.webSearchMode !== "disabled" ? ["web-search"] : []),
      ...(config.remoteToolPolicy === "full" ? ["workspace-write"] : []),
    ],
    metadata: {
      runtime: "dsh",
      runtime_version: "0.1.0-rc.5",
      remote_tool_policy: config.remoteToolPolicy,
      worker_session_mode: config.workerSessionMode,
    },
  });
  await hub.registerNode({
    node_id: config.nodeId,
    actor_id: config.actorId,
    display_name: config.nodeDisplayName,
    capabilities: [
      "filesystem",
      "remote-worker",
      ...nodeWebCapabilities(config),
    ],
    metadata: {
      platform: platform(),
      release: release(),
      workspace_root: config.workspaceRoot,
      runtime: "dsh",
      ...(nodeWebMetadata(config)
        ? { dsh_web: nodeWebMetadata(config) }
        : {}),
    },
  });
}

export function resolveDshIdentity(
  config: AgentHostConfig,
): AgentHostConfig {
  const actorId =
    process.env.AGENT_ACTOR_ID?.trim() ||
    (config.actorId.startsWith("pi-")
      ? `dsh-${config.actorId.slice("pi-".length)}`
      : `dsh-${config.actorId}`);
  const nodeId =
    process.env.AGENT_NODE_ID?.trim() || `dsh-${config.nodeId}`;
  return {
    ...config,
    actorId,
    actorDisplayName:
      process.env.AGENT_ACTOR_NAME?.trim() ||
      `DeepSeek Harness on ${hostname()}`,
    nodeId,
    nodeDisplayName: process.env.AGENT_NODE_NAME?.trim() || hostname(),
  };
}

export function resolveAdapterIdentity(
  config: AgentHostConfig,
  adapter: AdapterManifest,
): AgentHostConfig {
  const actorId =
    process.env.AGENT_ACTOR_ID?.trim() || `${adapter.id}-${config.nodeId}`;
  const nodeId =
    process.env.AGENT_NODE_ID?.trim() || `${config.nodeId}-${adapter.id}`;
  // A bridge registers its own node/actor pair, so a stored node token for
  // the plain node (or a user session token) does not match the bridge
  // identity. Drop both so resolveNodeCredential performs a fresh
  // agent-login for the adapter-specific node instead, and never overwrite
  // the plain node credential with the bridge token.
  const {
    hubNodeToken: _plainNodeToken,
    hubToken: _plainHubToken,
    ...base
  } = config;
  return {
    ...base,
    hubNodeTokenCacheDisabled: true,
    actorId,
    actorDisplayName:
      process.env.AGENT_ACTOR_NAME?.trim() ||
      `${adapter.display_name} on ${hostname()}`,
    nodeId,
    nodeDisplayName: process.env.AGENT_NODE_NAME?.trim() || hostname(),
  };
}

export async function registerAdapterHost(
  config: AgentHostConfig,
  hub: HubClient,
  adapter: AdapterManifest,
): Promise<void> {
  await hub.registerPrincipal({
    principal_id: config.principalId,
    kind: "human",
    display_name: config.principalDisplayName,
    metadata: {},
  });
  await hub.registerActor({
    actor_id: config.actorId,
    principal_id: config.principalId,
    kind: "agent",
    display_name: config.actorDisplayName,
    capabilities: [...adapter.capabilities, "hub-task"],
    metadata: {
      runtime: adapter.id,
      adapter: adapter.display_name,
    },
  });
  await hub.registerNode({
    node_id: config.nodeId,
    actor_id: config.actorId,
    display_name: config.nodeDisplayName,
    capabilities: ["filesystem", "remote-worker"],
    metadata: {
      platform: platform(),
      release: release(),
      workspace_root: config.workspaceRoot,
      adapter: adapter.id,
    },
  });
}
