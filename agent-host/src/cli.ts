#!/usr/bin/env node

import { dirname, resolve } from "node:path";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { homedir, userInfo } from "node:os";
import { Writable } from "node:stream";

import { runDshChild, startDshChild } from "./dsh-child.js";
import { WebBridge } from "./web-bridge.js";
import { buildDshCommonEnv, buildDshDispatchEnv, buildDshWorkerEnv } from "./dsh-env.js";
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
import { runDshDoctor } from "./dsh-doctor.js";
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

  const repositoryRoot = resolve(agentHostDir, "..");
  const pluginWorkerDefault =
    command === "worker" &&
    process.env.AGENT_WORKER_RUNTIME?.trim() !== "pi";
  const dshTuiDefault =
    (command === "tui" || command === "interactive") &&
    process.env.AGENT_TUI_RUNTIME?.trim() !== "pi";
  const config = loadConfig({
    allowDshPlugin: pluginWorkerDefault,
    allowDshTui: dshTuiDefault,
  });
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
    "web-bridge",
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
    } else if (
      !dshDispatchCommand &&
      !(command === "worker" && config.workerRuntime === "dsh-plugin")
    ) {
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
  if (command === "dsh-doctor") {
    await runDshDoctor(config, hub);
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

  if (command === "web") {
    await runDshWeb(config, hub);
    return;
  }

  if (command === "web-bridge") {
    await runWebBridge(config, hub!);
    return;
  }

  if (command === "interactive" || command === "tui" || command === "local") {
    if (command !== "local") {
      const started = await runDshTui(config, hub, repositoryRoot);
      if (started) return;
      console.warn(
        "DeepSeek Harness TUI is unavailable; falling back to the Pi TUI. " +
          "Run scripts/install-dsh-plugin.sh and keep the dsh-TUI checkout at " +
          resolve(repositoryRoot, "..", "dsh-TUI") + ".",
      );
    }
    await runInteractive(config, hub);
    return;
  }

  if (dshReceivingCommand) {
    const workerHub = hub!;
    const engine = await DshAgentEngine.create(workerConfig, workerHub);
    for (const warning of engine.diagnostics) {
      console.warn(warning);
    }
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
        for (const warning of engine.diagnostics) console.warn(warning);
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
    for (const warning of engine.diagnostics) console.warn(warning);
    await engine.dispose();
    return;
  }

  if (command === "worker" && config.workerRuntime === "dsh-plugin") {
    const started = await runDshPluginWorker(config, hub!, repositoryRoot);
    if (started) return;
    console.warn(
      "DeepSeek Harness plugin worker is unavailable; falling back to Pi. " +
        "Run scripts/install-dsh-plugin.sh to install the dsh worker profile.",
    );
    await registerHost(config, hub!);
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

async function runDshTui(
  config: AgentHostConfig,
  hub: HubClient | undefined,
  repositoryRoot: string,
): Promise<boolean> {
  const runtime = process.env.AGENT_TUI_RUNTIME?.trim();
  if (runtime !== undefined && runtime !== "dsh" && runtime !== "pi") {
    throw new Error("AGENT_TUI_RUNTIME must be dsh or pi");
  }
  if (runtime === "pi") return false;
  const forced = runtime === "dsh";
  // Prefer the combo-managed install when present (sources/dsh-tui sits next
  // to sources/deepseek-harness, so the sibling DSH_CHECKOUT inference below
  // resolves correctly); fall back to the standalone sibling checkout.
  const managedTui = resolve(
    homedir(),
    ".local",
    "share",
    "dsh-agent-society-combo",
    "sources",
    "dsh-tui",
  );
  const tuiRoot =
    process.env.AGENT_DSH_TUI_ROOT?.trim() ||
    (existsSync(resolve(managedTui, "scripts", "run.ts"))
      ? managedTui
      : resolve(repositoryRoot, "..", "dsh-TUI"));
  const runScript = resolve(tuiRoot, "scripts", "run.ts");
  const checkout =
    process.env.DSH_CHECKOUT?.trim() ||
    resolve(tuiRoot, "..", "deepseek-harness");
  const dshHome =
    process.env.DSH_HOME?.trim() || resolve(homedir(), ".dsh");
  const pluginPatch = resolve(
    dshHome,
    "plugins",
    "agent-society",
    "cordis.patch.yml",
  );
  const missing: string[] = [];
  if (!existsSync(runScript)) {
    missing.push(`dsh-TUI source launcher (${runScript})`);
  }
  if (!existsSync(resolve(checkout, "apps", "cli", "package.json"))) {
    missing.push(`DeepSeek Harness checkout (${checkout})`);
  }
  if (!existsSync(pluginPatch)) {
    missing.push(`AgentSociety dsh plugin link (${pluginPatch})`);
  }
  if (missing.length > 0) {
    const detail = missing.join("; ");
    if (forced) throw new Error(`Cannot start dsh TUI: ${detail}.`);
    console.warn(`Cannot start dsh TUI: ${detail}.`);
    return false;
  }

  const require = createRequire(resolve(tuiRoot, "package.json"));
  let tsxLoader: string;
  try {
    tsxLoader = require.resolve("tsx/esm");
  } catch (error) {
    const detail = `Cannot resolve tsx from ${tuiRoot}: ${error instanceof Error ? error.message : String(error)}`;
    if (forced) throw new Error(detail);
    console.warn(detail);
    return false;
  }

  const env: NodeJS.ProcessEnv = {
    ...buildDshCommonEnv(config, hub, { worker: false, hubMcp: Boolean(hub) }),
    DSH_CHECKOUT: checkout,
  };
  if (process.argv.includes("--resume")) {
    let sessionId = "";
    for (const dir of [".dsh-tui", ".dsh-cc"]) {
      try {
        sessionId = readFileSync(
          resolve(homedir(), dir, "resume.txt"),
          "utf8",
        ).trim();
        if (sessionId) break;
      } catch {
        // No resume marker is a normal cold-start case.
      }
    }
    if (sessionId) {
      env.DSH_TUI_RESUME_SESSION = sessionId;
      env.DSH_CC_RESUME_SESSION = sessionId;
    }
  }

  console.log(
    `Starting dsh TUI from ${tuiRoot} (Hub tools ${hub ? "enabled" : "disabled"}).`,
  );
  const result = await runDshChild(
    [process.execPath, "--import", pathToFileURL(tsxLoader).href, runScript],
    env,
    {
      // npm --prefix runs the launcher with cwd=agent-host; the TUI derives
      // its default workspace from its cwd, so restore the user's terminal
      // directory (INIT_CWD) or the session falls back to showing zero
      // resumable sessions.
      cwd: process.env.INIT_CWD?.trim() || process.cwd(),
      //
      onError: (error) => {
        const detail = `Could not start dsh TUI: ${error.message}`;
        if (forced) throw new Error(detail);
        console.warn(detail);
        return false;
      },
    },
  );
  return result.started;
}

function profileIncludesCorePlugin(profilePackage: string): boolean {
  try {
    const manifest = JSON.parse(readFileSync(profilePackage, "utf8")) as {
      dsh?: { profile?: { bundles?: unknown } };
      dependencies?: Record<string, unknown>;
    };
    const bundles = Array.isArray(manifest.dsh?.profile?.bundles)
      ? manifest.dsh.profile.bundles
      : [];
    return (
      bundles.includes("@agent-society/dsh-agent-society") ||
      Object.prototype.hasOwnProperty.call(
        manifest.dependencies ?? {},
        "@agent-society/dsh-agent-society",
      )
    );
  } catch {
    return false;
  }
}

const DSH_WEB_STARTUP_TIMEOUT_MS = 30_000;
const DSH_WEB_PROBE_TIMEOUT_MS = 750;

async function probeDshWeb(target: string): Promise<boolean> {
  try {
    const response = await fetch(target, {
      method: "GET",
      redirect: "manual",
      signal: AbortSignal.timeout(DSH_WEB_PROBE_TIMEOUT_MS),
    });
    await response.body?.cancel();
    return true;
  } catch {
    return false;
  }
}

async function waitForDshWeb(
  target: string,
  child: ReturnType<typeof startDshChild>,
): Promise<boolean> {
  const deadline = Date.now() + DSH_WEB_STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await probeDshWeb(target)) return true;
    const result = await Promise.race([
      child.exited.then(() => "exit" as const),
      new Promise<"retry">((resolve) => setTimeout(resolve, 250)),
    ]);
    if (result === "exit") return false;
  }
  return probeDshWeb(target);
}

async function advertiseDshWeb(
  config: AgentHostConfig,
  hub: HubClient | undefined,
): Promise<void> {
  if (!hub || !config.dshWebEnabled) return;
  try {
    await hub.updateNodeWeb(config.nodeId, {
      enabled: true,
      protocol_version: "1",
      profile: config.dshWebProfile ?? "agent-society-web",
      capabilities: ["session.read"],
    });
    console.log(
      `Advertised DSH Web capability to the Hub as ${config.nodeId}`,
    );
  } catch (error) {
    console.warn(
      `Could not advertise DSH Web capability (${error instanceof Error ? error.message : String(error)}); the Hub will not list this device.`,
    );
  }
}

async function runWebBridge(
  config: AgentHostConfig,
  hub: HubClient,
): Promise<void> {
  const target =
    process.env.AGENT_DSH_WEB_TARGET?.trim() || "http://127.0.0.1:3080";
  const dshWeb = await ensureDshWeb(config, hub, target);
  await advertiseDshWeb(config, hub);
  const bridge = new WebBridge({
    hubUrl: config.hubUrl!,
    nodeToken: hub.nodeToken,
    nodeId: config.nodeId,
    target,
  });
  let stopping = false;
  const stop = () => {
    if (stopping) return;
    stopping = true;
    bridge.stop();
    dshWeb?.stop();
  };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  console.log(
    `Starting DSH Web bridge: ${config.nodeId} -> ${target} via ${config.hubUrl}`,
  );
  if (dshWeb === undefined && process.env.AGENT_DSH_WEB_BRIDGE_START === "0") {
    console.warn(
      "Automatic local DSH Web startup is disabled (AGENT_DSH_WEB_BRIDGE_START=0); the bridge expects an existing server.",
    );
  }
  const bridgeRun = bridge.run();
  if (dshWeb !== undefined) {
    const result = await Promise.race([
      bridgeRun.then(() => "bridge" as const),
      dshWeb.exited.then(() => "dsh-web" as const),
    ]);
    if (result === "dsh-web" && !stopping) {
      bridge.stop();
      await bridgeRun;
      return;
    }
  } else {
    await bridgeRun;
  }
  stop();
}

async function ensureDshWeb(
  config: AgentHostConfig,
  hub: HubClient,
  target: string,
) {
  // Do not start a second server when the user already ran `agent web`.
  if (await probeDshWeb(target)) return undefined;
  if (process.env.AGENT_DSH_WEB_BRIDGE_START === "0") return undefined;
  const child = startDshWebChild(config, hub, target);
  const ready = await waitForDshWeb(target, child);
  if (!ready) {
    child.stop();
    await child.exited;
    throw new Error(
      `Local DSH Web did not become ready at ${target} within ${DSH_WEB_STARTUP_TIMEOUT_MS / 1000}s.`,
    );
  }
  console.log(`Local DSH Web is ready at ${target}`);
  return child;
}

function startDshWebChild(
  config: AgentHostConfig,
  hub: HubClient,
  target: string,
) {
  const dshHome =
    process.env.DSH_HOME?.trim() || resolve(homedir(), ".dsh");
  const profile = "agent-society-web";
  const profilePackage = resolve(dshHome, "profiles", profile, "package.json");
  const profileReady = profileIncludesCorePlugin(profilePackage);
  const legacyPatch = resolve(
    dirname(fileURLToPath(import.meta.url)),
    "..",
    "..",
    "dsh",
    "agent-society.dsh.yml",
  );
  const port = new URL(target).port || "3080";
  const command = profileReady
    ? [...(config.dshCommand ?? ["dsh"]), "--profile", profile, "--port", port]
    : [
        ...(config.dshCommand ?? ["dsh"]),
        "web",
        "--patch",
        legacyPatch,
        "--port",
        port,
      ];
  const env = buildDshCommonEnv(config, hub, {
    worker: false,
    hubMcp: true,
  });
  console.log(`Starting local dsh web for bridge: ${command.join(" ")}`);
  return startDshChild(command, env, {
    onError: (error) => {
      console.error(`Could not start dsh web for bridge: ${error.message}`);
      return false;
    },
    onExit: (code, signal) => {
      if (code !== 0 && signal !== "SIGTERM" && signal !== "SIGINT") {
        console.error(
          `Local dsh web exited before bridge shutdown (code ${code ?? "none"}, signal ${signal ?? "none"}).`,
        );
      }
      return false;
    },
  });
}


async function runDshWeb(
  config: AgentHostConfig,
  hub: HubClient | undefined,
): Promise<void> {
  if (hub && config.dshWebEnabled) {
    try {
      await hub.updateNodeWeb(config.nodeId, {
        enabled: true,
        protocol_version: "1",
        profile: config.dshWebProfile ?? "agent-society-web",
        capabilities: ["session.read"],
      });
      console.log(
        `Advertised DSH Web capability to the Hub as ${config.nodeId}`,
      );
    } catch (error) {
      console.warn(
        `Could not advertise DSH Web capability (${error instanceof Error ? error.message : String(error)}); the Hub will not list this device.`,
      );
    }
  }
  const dshHome =
    process.env.DSH_HOME?.trim() || resolve(homedir(), ".dsh");
  const profile = "agent-society-web";
  const profilePackage = resolve(
    dshHome,
    "profiles",
    profile,
    "package.json",
  );
  const profileReady = profileIncludesCorePlugin(profilePackage);
  const legacyPatch = resolve(
    dirname(fileURLToPath(import.meta.url)),
    "..",
    "..",
    "dsh",
    "agent-society.dsh.yml",
  );
  const webArgs = process.argv.slice(3);
  const command = profileReady
    ? [...(config.dshCommand ?? ["dsh"]), "--profile", profile, ...webArgs]
    : [...(config.dshCommand ?? ["dsh"]), "web", "--patch", legacyPatch, ...webArgs];
  if (!profileReady) {
    console.warn(
      existsSync(profilePackage)
        ? `dsh web profile "${profile}" does not include the AgentSociety bundle; using legacy patch: ${legacyPatch}`
        : `dsh web profile "${profile}" not found; using legacy patch: ${legacyPatch}`,
    );
  }
  console.log(`Starting dsh web: ${command.join(" ")}`);
  const env = buildDshCommonEnv(config, hub, {
    worker: false,
    hubMcp: Boolean(hub),
  });
  await runDshChild(command, env, {
    onError: (error) => {
      console.error(`Could not start dsh web: ${error.message}`);
      process.exitCode = 1;
      return true;
    },
  });
}

const DSH_SELF_UPDATE_EXIT_CODE = 75

async function runDshPluginWorker(
  config: AgentHostConfig,
  hub: HubClient,
  repositoryRoot: string,
): Promise<boolean> {
  const profile = config.dshPluginProfile ?? "agent-society-worker";
  const dshHome =
    process.env.DSH_HOME?.trim() || resolve(homedir(), ".dsh");
  const profilePackage = resolve(
    dshHome,
    "profiles",
    profile,
    "package.json",
  );
  if (!existsSync(profilePackage)) {
    console.warn(
      `dsh plugin profile "${profile}" not found at ${profilePackage}.`,
    );
    return false;
  }
  const command = [
    ...(config.dshCommand ?? ["dsh"]),
    "--profile",
    profile,
  ];
  console.log(
    `Starting DeepSeek Harness plugin worker: ${command.join(" ")}`,
  );
  const env = buildDshWorkerEnv(config, hub, repositoryRoot);
  for (;;) {
    const outcome = await runDshChild(command, env, {
      onError: (error) => {
        const code = (error as NodeJS.ErrnoException).code
        if (code === "ENOENT") {
          console.warn(`Could not start ${command[0]!}: ${error.message}.`)
          return false
        }
        console.error(`DeepSeek Harness plugin worker failed: ${error.message}`)
        process.exitCode = 1
        return true
      },
      onExit: (code) => {
        if (code === DSH_SELF_UPDATE_EXIT_CODE) {
          console.log("Self-update applied; restarting dsh plugin worker.")
          return true
        }
        return false
      },
    })
    if (!outcome.started) return false
    if (!outcome.restart) return true
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 1_000))
  }
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
  await runDshChild(command, buildDshDispatchEnv(config, hub), {
    onError: (error) => {
      console.error(`Could not start ${command[0]}: ${error.message}`);
      process.exitCode = 1;
      return false;
    },
  });
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
