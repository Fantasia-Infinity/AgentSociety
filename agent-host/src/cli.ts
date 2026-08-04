#!/usr/bin/env node

import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { listAdapterIds, loadAdapterManifest } from "./adapter-registry.js";
import { BridgeWorker } from "./bridge.js";
import { loadConfig, loadSessionDir } from "./config.js";
import { controlTask } from "./controller.js";
import { runDoctor } from "./doctor.js";
import { HubClient } from "./hub-client.js";
import {
  registerAdapterHost,
  registerHost,
  resolveAdapterIdentity,
} from "./host.js";
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

  const hubCommands = new Set([
    "register",
    "bridge",
    "observe",
    "attach",
    "steer",
    "follow-up",
    "cancel",
    "control",
    "once",
    "worker",
  ]);
  if (hubCommands.has(command) && !hub) {
    throw new Error(
      `The ${command} command requires Hub configuration. Run ./agent setup to add one.`,
    );
  }
  if (hub && command !== "bridge") await registerHost(config, hub);

  if (command === "register") {
    console.log(`Registered ${config.actorId} on ${config.nodeId}`);
    return;
  }
  if (command === "bridge") {
    const bridgeArgs = process.argv.slice(3);
    const adapterId =
      adapterArgument(bridgeArgs) || process.env.AGENT_HUB_ADAPTER?.trim();
    if (!adapterId) {
      throw new Error(
        "bridge requires --adapter <id> (or AGENT_HUB_ADAPTER). Available adapters: " +
          (process.env.AGENT_HUB_ADAPTER_DIR
            ? `${adapterList(process.env.AGENT_HUB_ADAPTER_DIR)} / builtin`
            : adapterList()),
      );
    }
    const adapter = loadAdapterManifest(
      adapterId,
      process.env.AGENT_HUB_ADAPTER_DIR,
    );
    const bridgeConfig = resolveAdapterIdentity(config, adapter);
    await registerAdapterHost(bridgeConfig, hub!, adapter);
    const workerHub = new HubClient(
      bridgeConfig.hubUrl!,
      bridgeConfig.hubNodeToken ?? bridgeConfig.hubToken!,
    );
    const runOnce = bridgeArgs.includes("--once");
    if (runOnce) {
      const worker = new BridgeWorker(bridgeConfig, workerHub, adapter);
      try {
        const worked = await worker.runOnce();
        console.log(worked ? "Processed one task" : "No matching task");
      } finally {
        await worker.dispose();
      }
      return;
    }
    const controller = new AbortController();
    const stop = () => controller.abort();
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
    const workers = Array.from(
      { length: bridgeConfig.workerConcurrency },
      (_, index) =>
        new BridgeWorker(
          bridgeConfig,
          workerHub,
          adapter,
          (message) =>
            index === 0
              ? console.log(message)
              : console.log(`[bridge ${index + 1}] ${message}`),
          index,
        ),
    );
    await Promise.all(
      workers.map((current) => current.runForever(controller.signal)),
    );
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
  if (command === "steer" || command === "follow-up") {
    const taskId = process.argv[3]?.trim();
    const message = process.argv.slice(4).join(" ").trim();
    if (!taskId || !message) {
      throw new Error(`${command} requires a task_id and message`);
    }
    const control = await hub!.createTaskControl(taskId, {
      actor_id: config.actorId,
      kind: command === "steer" ? "steer" : "follow_up",
      message,
    });
    console.log(`Queued ${control.kind} control ${control.control_id}`);
    return;
  }
  if (command === "control") {
    const taskId = process.argv[3]?.trim();
    if (!taskId) throw new Error("control requires a task_id");
    await controlTask(config, hub!, taskId);
    return;
  }
  if (command === "cancel") {
    const taskId = process.argv[3]?.trim();
    if (!taskId) throw new Error("cancel requires a task_id");
    const reason = process.argv.slice(4).join(" ").trim();
    const task = await hub!.cancelTask(taskId, {
      actor_id: config.actorId,
      ...(reason ? { reason } : {}),
    });
    console.log(`Task ${task.task_id} is ${task.status}`);
    return;
  }

  if (command === "interactive" || command === "tui" || command === "local") {
    await runInteractive(config, hub);
    return;
  }

  const workerHub = new HubClient(
    config.hubUrl!,
    config.hubNodeToken ?? config.hubToken!,
  );
  const engine = await PiAgentEngine.create(config, workerHub);
  const worker = new TaskWorker(config, workerHub, engine);
  if (command === "once") {
    try {
      const worked = await worker.runOnce();
      console.log(worked ? "Processed one task" : "No matching task");
    } finally {
      await worker.dispose();
    }
    return;
  }
  if (command === "worker") {
    const controller = new AbortController();
    const stop = () => controller.abort();
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
    const workers = Array.from(
      { length: config.workerConcurrency },
      (_, index) =>
        index === 0
          ? worker
          : new TaskWorker(
              config,
              workerHub,
              engine,
              (message) =>
                console.log(`[worker ${index + 1}] ${message}`),
              undefined,
              index,
            ),
    );
    await Promise.all(
      workers.map((current) => current.runForever(controller.signal)),
    );
    return;
  }
  throw new Error(`Unknown command: ${command}`);
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});

function adapterArgument(argv: string[]): string | undefined {
  for (const arg of argv) {
    if (arg.startsWith("--adapter=")) {
      const value = arg.slice("--adapter=".length).trim();
      if (value) return value;
    }
  }
  const index = argv.indexOf("--adapter");
  if (index >= 0) {
    const value = argv[index + 1]?.trim();
    if (value) return value;
  }
  return undefined;
}

function adapterList(extraDir?: string): string {
  return listAdapterIds(extraDir).join(", ");
}
