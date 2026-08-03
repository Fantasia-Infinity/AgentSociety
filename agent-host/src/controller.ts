import { createInterface } from "node:readline/promises";

import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";

export async function controlTask(
  config: AgentHostConfig,
  hub: HubClient,
  taskId: string,
): Promise<void> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    throw new Error("Task control requires an interactive terminal");
  }
  const terminal = createInterface({ input: process.stdin, output: process.stdout });
  console.log(
    [
      `Controlling ${taskId}`,
      "Enter text to steer immediately.",
      "Commands: /follow <text>, /status, /cancel [reason], /quit",
    ].join("\n"),
  );
  try {
    while (true) {
      const line = (await terminal.question("agent-control> ")).trim();
      if (!line) continue;
      if (line === "/quit" || line === "/exit") return;
      if (line === "/status") {
        const task = await hub.getTask(taskId);
        console.log(
          JSON.stringify(
            {
              status: task.status,
              executor_actor_id: task.executor_actor_id,
              executor_node_id: task.executor_node_id,
              result: task.result,
              error: task.error,
            },
            null,
            2,
          ),
        );
        continue;
      }
      if (line === "/cancel" || line.startsWith("/cancel ")) {
        const reason = line.slice("/cancel".length).trim();
        const task = await hub.cancelTask(taskId, {
          actor_id: config.actorId,
          ...(reason ? { reason } : {}),
        });
        console.log(`Task is ${task.status}`);
        continue;
      }
      const follow = line.startsWith("/follow ");
      const message = follow ? line.slice("/follow ".length).trim() : line;
      if (!message) continue;
      const control = await hub.createTaskControl(taskId, {
        actor_id: config.actorId,
        kind: follow ? "follow_up" : "steer",
        message,
      });
      console.log(`Queued ${control.kind}: ${control.control_id}`);
    }
  } finally {
    terminal.close();
  }
}
