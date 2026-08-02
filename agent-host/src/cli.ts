#!/usr/bin/env node

import { loadConfig, loadSessionDir } from "./config.js";
import { HubClient } from "./hub-client.js";
import { registerHost } from "./host.js";
import { runInteractive, runInteractiveChild } from "./interactive.js";
import { observeRun } from "./observer.js";
import { PiAgentEngine } from "./pi-engine.js";
import { RunSessionRegistry } from "./run-registry.js";
import { TaskWorker } from "./worker.js";

async function main(): Promise<void> {
  const command = process.argv[2] ?? "interactive";
  if (command === "sessions") {
    const records = new RunSessionRegistry(loadSessionDir()).list();
    if (!records.length) {
      console.log("No local Agent Host sessions");
      return;
    }
    for (const record of records) {
      console.log(
        [
          record.status.padEnd(9),
          record.runId,
          record.taskId ?? "local",
          record.sessionId,
        ].join("  "),
      );
    }
    return;
  }

  const config = loadConfig();
  const hub = new HubClient(config.hubUrl, config.hubToken);
  if (command === "__tui-child") {
    const runId = process.argv[3]?.trim();
    if (!runId) throw new Error("Internal TUI child requires a run_id");
    const engine = await PiAgentEngine.create(config, hub);
    await runInteractiveChild(config, engine, runId);
    return;
  }

  await registerHost(config, hub);

  if (command === "register") {
    console.log(`Registered ${config.actorId} on ${config.nodeId}`);
    return;
  }
  if (command === "observe" || command === "attach") {
    const id = process.argv[3]?.trim();
    if (!id) throw new Error(`${command} requires a run_id or task_id`);
    await observeRun(config, hub, id);
    return;
  }

  if (command === "interactive" || command === "tui") {
    await runInteractive(config, hub);
    return;
  }

  const engine = await PiAgentEngine.create(config, hub);
  const worker = new TaskWorker(config, hub, engine);
  if (command === "once") {
    const worked = await worker.runOnce();
    console.log(worked ? "Processed one task" : "No matching task");
    return;
  }
  if (command === "worker") {
    const controller = new AbortController();
    const stop = () => controller.abort();
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
    await worker.runForever(controller.signal);
    return;
  }
  throw new Error(`Unknown command: ${command}`);
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
