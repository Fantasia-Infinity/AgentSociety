import { accessSync, constants, mkdirSync, statSync } from "node:fs";

import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";
import { PiAgentEngine } from "./pi-engine.js";
import { createWebSearchProvider, webSearchStatus } from "./web-search.js";

export async function runDoctor(
  config: AgentHostConfig,
  hub?: HubClient,
): Promise<void> {
  if (!statSync(config.workspaceRoot).isDirectory()) {
    throw new Error("AGENT_WORKSPACE_ROOT must be a directory");
  }
  accessSync(config.workspaceRoot, constants.R_OK | constants.W_OK);
  mkdirSync(config.sessionDir, { recursive: true, mode: 0o700 });
  accessSync(config.sessionDir, constants.R_OK | constants.W_OK);

  const engine = await PiAgentEngine.create(config, hub);
  const conversation = await engine.createConversation({
    cwd: config.workspaceRoot,
    mode: "diagnostic",
    persisted: false,
  });
  try {
    const response = await conversation.prompt(
      "Connection check only. Reply with exactly AGENT_SETUP_OK and do not call tools.",
    );
    if (!response.text.trim()) {
      throw new Error("The remote model returned an empty response");
    }
  } finally {
    await conversation.dispose();
  }

  console.log(
    hub
      ? `Hub connection:   ok (${config.hubUrl})`
      : "Hub connection:   disabled (local Agent mode)",
  );
  console.log(`Agent identity:   ${config.actorId} on ${config.nodeId}`);
  console.log(`Workspace:        ok (${config.workspaceRoot})`);
  console.log(`Session storage:  ok (${config.sessionDir})`);
  console.log(`Remote model:     ok (${config.remoteModel ?? config.piModel})`);
  console.log(
    `Web search:       ${webSearchStatus(config.webSearchMode, createWebSearchProvider(config))}`,
  );
  console.log(
    config.builtinCapabilitiesEnabled
      ? "Agent tools:      ok (subagent, plan/todo, memory, LSP, MCP, background)"
      : "Agent tools:      disabled (AGENT_BUILTIN_CAPABILITIES=0)",
  );
  console.log(
    `Worker sessions:  ${config.workerSessionMode}`,
  );
}
