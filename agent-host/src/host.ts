import { platform, release } from "node:os";

import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";

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
    },
  });
  await hub.registerNode({
    node_id: config.nodeId,
    actor_id: config.actorId,
    display_name: config.nodeDisplayName,
    capabilities: ["filesystem", "local-interactive", "remote-worker"],
    metadata: {
      platform: platform(),
      release: release(),
      workspace_root: config.workspaceRoot,
    },
  });
}
