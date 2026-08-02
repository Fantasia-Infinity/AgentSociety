#!/usr/bin/env node

import { loadConfig } from "./config.js";
import { HubClient } from "./hub-client.js";
import { registerHost } from "./host.js";
import { runInteractive } from "./interactive.js";
import { PiAgentEngine } from "./pi-engine.js";
import { TaskWorker } from "./worker.js";

async function main(): Promise<void> {
  const command = process.argv[2] ?? "interactive";
  const config = loadConfig();
  const hub = new HubClient(config.hubUrl, config.hubToken);
  await registerHost(config, hub);

  if (command === "register") {
    console.log(`Registered ${config.actorId} on ${config.nodeId}`);
    return;
  }

  const engine = await PiAgentEngine.create(config, hub);
  if (command === "interactive") {
    await runInteractive(config, hub, engine);
    return;
  }

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
