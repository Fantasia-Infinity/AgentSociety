#!/usr/bin/env node

import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { userInfo } from "node:os";
import { Writable } from "node:stream";

import { sanitizedChildEnv } from "./child-env.js";
import { listAdapterIds, loadAdapterManifest } from "./adapter-registry.js";
import { BridgeWorker } from "./bridge.js";
import { agentHubProjectDir } from "./codex-project.js";
import { agentEnvPath, loadConfig, loadSessionDir } from "./config.js";
import type { AgentHostConfig } from "./config.js";
import { controlTask } from "./controller.js";
import {
  deleteSystemCredential,
  writeSystemCredential,
} from "./credential-store.js";
import { DshAgentEngine } from "./dsh-engine.js";
import { runDoctor } from "./doctor.js";
import { HubClient, HubError } from "./hub-client.js";
import {
  registerAdapterHost,
  registerDshHost,
  registerHost,
  resolveAdapterIdentity,
  resolveDshIdentity,
} from "./host.js";
import { runInteractive, runInteractiveChild } from "./interactive.js";
import { observeRun } from "./observer.js";
import { PiAgentEngine } from "./pi-engine.js";
import { RunSessionRegistry } from "./run-registry.js";
import { applyPendingUpdate } from "./self-update.js";
import { DSH_ENGINE_PROFILE } from "./types.js";
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
  if (
    process.env.AGENT_HUB_RECEIVE_DISABLED?.trim() === "1" &&
    new Set(["worker", "bridge", "once", "dsh-worker", "dsh-once"]).has(
      command,
    )
  ) {
    throw new Error(
      "Receiving tasks is disabled on this host (AGENT_HUB_RECEIVE_DISABLED=1). " +
        "This machine is configured as dispatch-only.",
    );
  }
  if (command === "connect") {
    await connectCommand(config);
    return;
  }
  const dshReceivingCommand =
    command === "dsh-worker" || command === "dsh-once";
  const dshDispatchCommand = command === "dsh-dispatch";
  const workerConfig = dshReceivingCommand
    ? resolveDshIdentity(config)
    : config;
  const hub =
    config.hubEnabled && !hubRuntimeDisabled
      ? await resolveHubClient(workerConfig, dshReceivingCommand ? "dsh" : "pi")
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
    "dsh-once",
    "dsh-worker",
    "dsh-dispatch",
  ]);
  if (hubCommands.has(command) && !hub) {
    throw new Error(
      `The ${command} command requires Hub configuration. Run ./agent setup to add one.`,
    );
  }
  if (
    hub &&
    command !== "bridge" &&
    workerConfig.hubToken &&
    !workerConfig.hubUsername
  ) {
    if (dshReceivingCommand) {
      await registerDshHost(workerConfig, hub);
    } else if (!dshDispatchCommand) {
      await registerHost(config, hub);
    }
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
    let adapter = loadAdapterManifest(
      adapterId,
      process.env.AGENT_HUB_ADAPTER_DIR,
    );
    if (adapter.id === "dsh" && process.env.AGENT_DSH_COMMAND?.trim()) {
      adapter = {
        ...adapter,
        command: [...(config.dshCommand ?? ["dsh"]), "--profile", "headless"],
      };
    }
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

  if (command === "dsh-dispatch") {
    await runDshDispatch(config, hub!);
    return;
  }

  if (command === "interactive" || command === "tui" || command === "local") {
    await runInteractive(config, hub);
    return;
  }

  if (dshReceivingCommand) {
    const workerHub = hub!;
    const engine = await DshAgentEngine.create(workerConfig, workerHub);
    const worker = new TaskWorker(
      workerConfig,
      workerHub,
      engine,
      console.log,
      undefined,
      0,
      DSH_ENGINE_PROFILE,
    );
    if (command === "dsh-once") {
      try {
        const worked = await worker.runOnce();
        console.log(worked ? "Processed one task" : "No matching task");
      } finally {
        await worker.dispose();
        await engine.dispose();
      }
      return;
    }
    const controller = new AbortController();
    const stop = () => controller.abort();
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
    const workers = Array.from(
      { length: workerConfig.workerConcurrency },
      (_, index) =>
        index === 0
          ? worker
          : new TaskWorker(
              workerConfig,
              workerHub,
              engine,
              (message) =>
                console.log(`[DeepSeek Harness worker ${index + 1}] ${message}`),
              undefined,
              index,
              DSH_ENGINE_PROFILE,
            ),
    );
    await Promise.all(
      workers.map((current) => current.runForever(controller.signal)),
    );
    await engine.dispose();
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

async function runDshDispatch(
  config: AgentHostConfig,
  hub: HubClient,
): Promise<void> {
  const patchPath = resolve(
    dirname(fileURLToPath(import.meta.url)),
    "..",
    "..",
    "dsh",
    "agent-society.dsh.yml",
  );
  if (!existsSync(patchPath)) {
    throw new Error(`DeepSeek Harness dispatch patch not found: ${patchPath}`);
  }
  const command = [
    ...(config.dshCommand ?? ["dsh"]),
    "web",
    "--patch",
    patchPath,
    ...process.argv.slice(3),
  ];
  console.log(
    `Starting DeepSeek Harness web with AgentSociety Hub MCP tools (${command.join(" ")})`,
  );
  const child = spawn(command[0]!, command.slice(1), {
    stdio: "inherit",
    env: {
      ...sanitizedChildEnv(process.env),
      AGENT_SOCIETY_HUB_URL: config.hubUrl!,
      AGENT_SOCIETY_HUB_MCP_TOKEN: hub.nodeToken,
    },
  });
  const exitCode = await new Promise<number | null>((resolveExit) => {
    child.once("error", (error) => {
      console.error(`Could not start ${command[0]}: ${error.message}`);
      resolveExit(null);
    });
    child.once("exit", (code) => resolveExit(code));
  });
  if (exitCode !== 0) process.exitCode = exitCode ?? 1;
}

type WorkerRuntimeKind = "pi" | "dsh";

function hostCapabilities(
  config: AgentHostConfig,
  kind: WorkerRuntimeKind = "pi",
): string[] {
  if (kind === "dsh") {
    return [
      "dsh",
      "code",
      "hub-task",
      ...(config.webSearchMode !== "disabled" ? ["web-search"] : []),
      ...(config.remoteToolPolicy === "full" ? ["workspace-write"] : []),
    ];
  }
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
  kind: WorkerRuntimeKind = "pi",
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
        capabilities: hostCapabilities(config, kind),
        metadata: {
          origin: "worker-boot",
          workspace_root: config.workspaceRoot,
          runtime: kind,
          runtime_version: kind === "dsh" ? "0.1.0-rc.5" : "0.83.0",
          remote_tool_policy: config.remoteToolPolicy,
          builtin_capabilities: config.builtinCapabilitiesEnabled,
          worker_session_mode: config.workerSessionMode,
        },
      });
    } catch (error) {
      if (
        error instanceof HubError &&
        (error.status === 401 || error.status === 409)
      ) {
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
    if (!config.hubNodeTokenCacheDisabled) {
      try {
        writeSystemCredential(service, account, login.node_token, "Hub node credential");
      } catch (error) {
        saved = false;
        console.warn(
          `System credential store unavailable (${error instanceof Error ? error.message : String(error)}); keeping the node credential in memory only.`,
        );
      }
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
  kind: WorkerRuntimeKind = "pi",
): Promise<HubClient> {
  const { token } = await resolveNodeCredential(config, kind);
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
    const muted = new MutedOutput();
    const rl = createInterface({ input: stdin, output: muted, terminal: true });
    const answer = (
      await rl.question(`Hub username [${username || "required"}]: `)
    ).trim();
    if (answer) username = answer;
    process.stdout.write(
      password
        ? "Hub password [configured; Enter keeps it]: "
        : "Hub password: ",
    );
    muted.muted = true;
    const entered = await rl.question("");
    muted.muted = false;
    process.stdout.write("\n");
    if (entered) password = entered;
    rl.close();
  }
  if (!username || !password) {
    throw new Error(
      "Hub username and password are required. Run `agent connect` interactively or set AGENT_HUB_USERNAME/AGENT_HUB_PASSWORD.",
    );
  }
  const resolved = { ...config, hubUsername: username, hubPassword: password };
  const nodeService =
    process.env.AGENT_HUB_NODE_TOKEN_CREDENTIAL_SERVICE?.trim() ||
    "AgentSociety Hub Node";
  const nodeAccount =
    process.env.AGENT_HUB_NODE_TOKEN_CREDENTIAL_ACCOUNT?.trim() ||
    userInfo().username;
  let token: string;
  try {
    const login = await new HubClient(resolved.hubUrl!, "").agentLogin({
      username,
      password,
      node_id: config.nodeId,
      actor_id: config.actorId,
      display_name: config.nodeDisplayName,
      capabilities: hostCapabilities(config),
      metadata: {
        origin: "agent-connect",
        workspace_root: config.workspaceRoot,
        runtime: "pi",
        runtime_version: "0.83.0",
        remote_tool_policy: config.remoteToolPolicy,
        builtin_capabilities: config.builtinCapabilitiesEnabled,
        worker_session_mode: config.workerSessionMode,
      },
    });
    token = login.node_token;
  } catch (error) {
    if (
      error instanceof HubError &&
      (error.status === 401 || error.status === 409)
    ) {
      throw new Error(
        "Hub rejected your credentials. Check your username and password and try again.",
      );
    }
    throw error;
  }
  const envPath = agentEnvPath(
    resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", ".."),
  );
  mkdirSync(dirname(envPath), { recursive: true });
  ensureEnvLine(
    envPath,
    "AGENT_HUB_NODE_TOKEN_CREDENTIAL_SERVICE",
    nodeService,
  );
  ensureEnvLine(envPath, "AGENT_HUB_NODE_TOKEN_CREDENTIAL_ACCOUNT", nodeAccount);
  let saved = false;
  try {
    writeSystemCredential(nodeService, nodeAccount, token, "Hub node credential");
    saved = true;
  } catch (error) {
    console.warn(
      `Could not save the node credential to the system store (${error instanceof Error ? error.message : String(error)}); ` +
        "worker restarts may need to run connect again.",
    );
  }
  try {
    const passwordService =
      process.env.AGENT_HUB_PASSWORD_CREDENTIAL_SERVICE?.trim() ||
      "AgentSociety Hub Password";
    writeSystemCredential(passwordService, username, password, "Hub password");
  } catch (error) {
    console.warn(
      `Could not save the Hub password to the system store (${error instanceof Error ? error.message : String(error)}); worker restarts may need to run connect again.`,
    );
  }
  console.log(
    `Connected to Hub as ${username} on node ${config.nodeId} (${config.actorId})`,
  );
  if (saved) {
    console.log("Node credential saved to the system credential store.");
  } else {
    console.warn(
      "The system credential store is unavailable on this machine. " +
        "Worker restarts may need to run connect again. " +
        "The node credential is intentionally not printed here.",
    );
  }
}

function ensureEnvLine(path: string, key: string, value: string): void {
  const text = existsSync(path) ? readFileSync(path, "utf8") : "";
  const lines = text.split(/\r?\n/u);
  const out: string[] = [];
  let replaced = false;
  for (const line of lines) {
    const match = line.match(/^([A-Z][A-Z0-9_]*)\s*=/u);
    if (match && match[1] === key) {
      out.push(`${key}="${value}"`);
      replaced = true;
    } else {
      out.push(line);
    }
  }
  if (!replaced) out.push(`${key}="${value}"`);
  writeFileSync(path, `${out.join("\n").replace(/\n+$/u, "")}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
}

class MutedOutput extends Writable {
  muted = false;

  _write(
    chunk: Buffer | string,
    _encoding: BufferEncoding,
    callback: (error?: Error | null) => void,
  ): void {
    if (!this.muted) {
      process.stdout.write(chunk, _encoding);
    }
    callback();
  }
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
