import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";

import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";
import type { AgentEngine } from "./types.js";

export async function runInteractive(
  config: AgentHostConfig,
  hub: HubClient,
  engine: AgentEngine,
): Promise<void> {
  const run = await hub.startRun({
    principal_id: config.principalId,
    actor_id: config.actorId,
    node_id: config.nodeId,
    origin: "local_ui",
    objective: "Interactive session controlled by the signed-in local user",
    metadata: { client: "terminal" },
  });
  const conversation = await engine.createConversation({
    cwd: config.workspaceRoot,
    mode: "local",
    persisted: true,
  });
  const readline = createInterface({ input: stdin, output: stdout, terminal: true });
  let lastText = "";
  stdout.write(
    `Pi Agent Host (${config.actorId})\nWorkspace: ${config.workspaceRoot}\nType /exit to finish.\n`,
  );
  try {
    while (true) {
      const input = (await readline.question("\nyou> ")).trim();
      if (!input) continue;
      if (input === "/exit" || input === "/quit") break;
      stdout.write("pi> ");
      const result = await conversation.prompt(input, (delta) => stdout.write(delta));
      lastText = result.text;
      stdout.write("\n");
    }
    await hub.updateRun(run.run_id, {
      status: "completed",
      result: { last_text: lastText },
    });
  } catch (error) {
    await hub.updateRun(run.run_id, {
      status: "failed",
      result: {},
      error: error instanceof Error ? error.message : String(error),
    });
    throw error;
  } finally {
    readline.close();
    conversation.dispose();
  }
}
