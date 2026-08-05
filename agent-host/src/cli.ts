#!/usr/bin/env node

import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { userInfo } from "node:os";
import { Writable } from "node:stream";

import { listAdapterIds, loadAdapterManifest } from "./adapter-registry.js";
import { BridgeWorker } from "./bridge.js";
import { agentHubProjectDir } from "./codex-project.js";
import { loadConfig, loadSessionDir } from "./config.js";
import type { AgentHostConfig } from "./config.js";
import { controlTask } from "./controller.js";
import {
  deleteSystemCredential,
  writeSystemCredential,
} from "./credential-store.js";
import { runDoctor } from "./doctor.js";
import { HubClient, HubError } from "./hub-client.js";
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
  if (command === "connect") {
    await connectCommand(config);
    return;
  }
  const hub =
    config.hubEnabled && !hubRuntimeDisabled
      ? await resolveHubClient(config)
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
  if (
    hub &&
    command !== "bridge" &&
    config.hubToken &&
    !config.hubUsername
  ) {
    await registerHost(config, hub);
  }

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
    let bridgeConfig = resolveAdapterIdentity(config, adapter);
    if (
      adapter.id === "codex" &&
      !process.env.AGENT_WORKSPACE_ROOT?.trim()
    ) {
      // Hub Codex sessions default to the unified AgentHub workspace so they
      // live inside the registered project directory. Set
      // AGENT_WORKSPACE_ROOT explicitly to use another workspace.
      bridgeConfig = { ...bridgeConfig, workspaceRoot: agentHubProjectDir() };
    }
    await registerAdapterHost(bridgeConfig, hub!, adapter);
    const workerHub = await resolveHubClient(bridgeConfig);
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

  const workerHub = hub!;
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

function hostCapabilities(config: AgentHostConfig): string[] {
  return [
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
  ];
}

async function resolveNodeCredential(
  config: AgentHostConfig,
): Promise<{ token: string; saved: boolean }> {
  if (config.hubNodeToken) {
    return { token: config.hubNodeToken, saved: true };
  }
  if (config.hubUsername && config.hubPassword) {
    let login;
    try {
      login = await new HubClient(config.hubUrl!, "").agentLogin({
        username: config.hubUsername,
        password: config.hubPassword,
        node_id: config.nodeId,
        actor_id: config.actorId,
        display_name: config.nodeDisplayName,
        capabilities: hostCapabilities(config),
        metadata: {
          origin: "worker-boot",
          workspace_root: config.workspaceRoot,
          runtime: "pi",
          runtime_version: "0.83.0",
          remote_tool_policy: config.remoteToolPolicy,
          builtin_capabilities: config.builtinCapabilitiesEnabled,
          worker_session_mode: config.workerSessionMode,
        },
      });
    } catch (error) {
      if (error instanceof HubError && error.status === 401) {
        throw new Error(
          "Hub rejected your credentials. If you changed your password, run `agent connect` and enter the new password.",
        );
      }
      throw error;
    }
    const service =
      process.env.AGENT_HUB_NODE_TOKEN_CREDENTIAL_SERVICE?.trim() ||
      "AgentSociety Hub Node";
    const account =
      process.env.AGENT_HUB_NODE_TOKEN_CREDENTIAL_ACCOUNT?.trim() ||
      userInfo().username;
    let saved = true;
    try {
      writeSystemCredential(service, account, login.node_token, "Hub node credential");
    } catch (error) {
      saved = false;
      console.warn(
        `System credential store unavailable (${error instanceof Error ? error.message : String(error)}); keeping the node credential in memory only.`,
      );
    }
    return { token: login.node_token, saved };
  }
  if (config.hubToken) {
    return { token: config.hubToken, saved: true };
  }
  throw new Error(
    "Hub is not configured with a password account or token. Run ./agent setup.",
  );
}

async function resolveHubClient(
  config: AgentHostConfig,
): Promise<HubClient> {
  const { token } = await resolveNodeCredential(config);
  return new HubClient(config.hubUrl!, token);
}

async function connectCommand(config: AgentHostConfig): Promise<void> {
  if (!config.hubEnabled || !config.hubUrl) {
    throw new Error("Hub is not configured. Run ./agent setup.");
  }
  let username = config.hubUsername ?? "";
  let password = config.hubPassword ?? "";
  const interactive = Boolean(stdin.isTTY && stdout.isTTY);
  if (interactive) {
    const muted = new Writable({
      write(_chunk, _encoding, callback) {
        callback();
      },
    });
    const rl = createInterface({ input: stdin, output: muted, terminal: true });
    const answer = (
      await rl.question(`Hub username [${username || "required"}]: `)
    ).trim();
    if (answer) username = answer;
    password = await rl.question("Hub password: ");
    process.stdout.write("\n");
    rl.close();
  }
  if (!username || !password) {
    throw new Error(
      "Hub username and password are required. Run `agent connect` interactively or set AGENT_HUB_USERNAME/AGENT_HUB_PASSWORD.",
    );
  }
  const resolved = { ...config, hubUsername: username, hubPassword: password };
  const { token, saved } = await resolveNodeCredential(resolved);
  const hub = new HubClient(resolved.hubUrl!, token);
  console.log(
    `Connected to Hub as ${username} on node ${config.nodeId} (${config.actorId})`,
  );
  if (saved) {
    console.log("Node credential saved to the system credential store.");
  } else {
    console.warn(
      "The system credential store is unavailable on this machine. " +
        `Set AGENT_HUB_NODE_TOKEN=${token} in .env.agent to persist the node credential.`,
    );
  }
}
