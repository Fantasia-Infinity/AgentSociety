#!/usr/bin/env node

import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadConfig, loadSessionDir } from "./config.js";
import { runDoctor } from "./doctor.js";
import { HubClient } from "./hub-client.js";
import { registerHost } from "./host.js";
import { runInteractive, runInteractiveChild } from "./interactive.js";
import { observeRun } from "./observer.js";
import { PiAgentEngine } from "./pi-engine.js";
import { RunSessionRegistry } from "./run-registry.js";
import { applyPendingUpdate } from "./self-update.js";
import { TaskWorker } from "./worker.js";

async function main(): Promise<void> {
  const command = process.argv[2] ?? "interactive";
  // A self-update that deferred npm ci (because the previous worker process
  // held Windows DLL locks on node_modules) is applied here, before any
  // credentials load: the keyring native addon is not loaded yet, so npm ci
  // can replace the tree. Failures keep the marker for the next start.
  const agentHostDir = resolve(
    dirname(fileURLToPath(import.meta.url)),
    "..",
    "..",
  );
  applyPendingUpdate(agentHostDir);
  if (command === "local") process.env.AGENT_HUB_RUNTIME_DISABLED = "1";
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
  const hubRuntimeDisabled = process.env.AGENT_HUB_RUNTIME_DISABLED === "1";
  const hub = config.hubEnabled && !hubRuntimeDisabled
    ? new HubClient(config.hubUrl!, config.hubToken!)
    : undefined;
  if (command === "__tui-child") {
    const runId = process.argv[3]?.trim();
    if (!runId) throw new Error("Internal TUI child requires a run_id");
    const engine = await PiAgentEngine.create(config, hub);
    await runInteractiveChild(config, engine, runId);
    return;
  }

  const hubCommands = new Set(["register", "observe", "attach", "once", "worker"]);
  if (hubCommands.has(command) && !hub) {
    throw new Error(
      `The ${command} command requires Hub configuration. Run ./agent setup to add one.`,
    );
  }
  if (hub) await registerHost(config, hub);

  if (command === "register") {
    console.log(`Registered ${config.actorId} on ${config.nodeId}`);
    return;
  }
  if (command === "doctor") {
    await runDoctor(config, hub);
    return;
  }
  if (command === "observe" || command === "attach") {
    const id = process.argv[3]?.trim();
    if (!id) throw new Error(`${command} requires a run_id or task_id`);
    await observeRun(config, hub!, id);
    return;
  }

  if (command === "interactive" || command === "tui" || command === "local") {
    await runInteractive(config, hub);
    return;
  }

  const engine = await PiAgentEngine.create(config, hub);
  const worker = new TaskWorker(config, hub!, engine);
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
