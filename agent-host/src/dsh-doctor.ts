import { accessSync, constants, mkdirSync, statSync } from "node:fs";

import type { AgentHostConfig } from "./config.js";
import { DshAgentEngine } from "./dsh-engine.js";
import type { HubClient } from "./hub-client.js";

export async function runDshDoctor(
  config: AgentHostConfig,
  hub?: HubClient,
): Promise<void> {
  if (!statSync(config.workspaceRoot).isDirectory()) {
    throw new Error("AGENT_WORKSPACE_ROOT must be a directory");
  }
  accessSync(config.workspaceRoot, constants.R_OK | constants.W_OK);
  mkdirSync(config.sessionDir, { recursive: true, mode: 0o700 });
  accessSync(config.sessionDir, constants.R_OK | constants.W_OK);

  const engine = await DshAgentEngine.create(config, hub);
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
      throw new Error("The DeepSeek Harness model returned an empty response");
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
  console.log(
    `Session storage:  ok (${config.dshSessionRoot ?? config.sessionDir})`,
  );
  console.log(`Runtime model:    ok (${engine.modelName})`);
  console.log(
    `Hub MCP tools:    ${
      !hub
        ? "disabled (no Hub configured)"
        : engine.diagnostics.some((item) => item.includes("dsh-mcp-client"))
          ? "disabled (plugin unavailable)"
          : "enabled"
    }`,
  );
  console.log(
    `Web search:       ${
      config.dshWebSearch === false
        ? "disabled"
        : engine.diagnostics.some((item) => item.includes("web-search"))
          ? "disabled (plugin unavailable)"
          : "enabled"
    }`,
  );
  console.log(`Worker sessions:  ${config.workerSessionMode}`);
  for (const warning of engine.diagnostics) {
    console.warn(`Warning:          ${warning}`);
  }
  await engine.dispose();
}
