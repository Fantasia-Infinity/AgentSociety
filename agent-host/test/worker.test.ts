import assert from "node:assert/strict";
import { Entry } from "@napi-rs/keyring";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { afterEach, test } from "node:test";

import {
  assertRemoteUrl,
  discoverProjectEnv,
  resolveHubConfig,
  type AgentHostConfig,
} from "../src/config.js";
import { RunSessionRegistry } from "../src/run-registry.js";
import {
  DSH_ENGINE_PROFILE,
  type AgentConversation,
  type AgentEngine,
  type HubClaim,
  type HubTask,
} from "../src/types.js";
import { resolveTaskWorkspace, TaskWorker } from "../src/worker.js";

const temporaryDirectories: string[] = [];
const temporaryCredentials: Entry[] = [];
afterEach(() => {
  for (const path of temporaryDirectories.splice(0)) {
    rmSync(path, { recursive: true, force: true });
  }
  for (const entry of temporaryCredentials.splice(0)) {
    try {
      entry.deletePassword();
    } catch {
      // A failed setup may not have created the credential.
    }
  }
});

function temporaryDirectory(): string {
  const path = mkdtempSync(join(tmpdir(), "agent-host-test-"));
  temporaryDirectories.push(path);
  return path;
}

function temporaryCredentialNamespace(): {
  namespace: string;
  account: string;
} {
  return {
    namespace: `AgentSociety Test ${randomUUID()}`,
    account: `test-${randomUUID()}`,
  };
}

function trackedCredential(service: string, account: string): Entry {
  const entry = new Entry(service, account);
  temporaryCredentials.push(entry);
  return entry;
}

function config(workspaceRoot: string): AgentHostConfig {
  return {
    hubEnabled: true,
    hubUrl: "http://127.0.0.1:8090",
    hubToken: "test-token",
    principalId: "principal-owner",
    principalDisplayName: "Owner",
    actorId: "actor-pi",
    actorDisplayName: "Pi",
    nodeId: "node-mac",
    nodeDisplayName: "Mac",
    workspaceRoot,
    sessionDir: join(workspaceRoot, ".sessions"),
    pollSeconds: 1,
    leaseSeconds: 300,
    workerConcurrency: 1,
    workerSupervised: false,
    workerSessionMode: "per_task",
    workerSessionMaxTasks: 0,
    workerSessionMaxAgeHours: 0,
    remoteToolPolicy: "read_only",
    remotePiResourcePolicy: "disabled",
    selfUpdateEnabled: true,
    builtinCapabilitiesEnabled: true,
    subagentMaxDepth: 2,
    subagentConcurrency: 4,
    backgroundMaxProcesses: 8,
    webSearchMode: "disabled",
    webSearchModel: "deepseek-v4-flash",
    remoteBaseUrl: "https://models.example/v1",
    remoteApiKey: "key",
    remoteModel: "model",
    contextWindow: 100_000,
    maxOutputTokens: 4_096,
    thinkingLevel: "off",
  };
}

function task(workspace = "."): HubTask {
  return {
    task_id: "task-1",
    context_id: null,
    principal_id: "principal-owner",
    delegator_actor_id: "actor-owner",
    assignee_actor_id: "actor-pi",
    objective: "Inspect tests",
    required_capabilities: [],
    input: { workspace },
    metadata: {},
    origin: "hub",
    status: "working",
    result: {},
    error: null,
  };
}

function claim(): HubClaim {
  return {
    task: task(),
    run: {
      run_id: "run-1",
      task_id: "task-1",
      principal_id: "principal-owner",
      actor_id: "actor-pi",
      node_id: "node-mac",
      origin: "remote_task",
      objective: "Inspect tests",
      status: "active",
      result: {},
      error: null,
    },
    lease_token: "lease-token",
  };
}

test("worker completes a claimed task through the agent engine", async () => {
  const workspace = temporaryDirectory();
  const updates: Array<Record<string, unknown>> = [];
  const hub = {
    claimTask: async () => claim(),
    updateTask: async (_taskId: string, update: Record<string, unknown>) => {
      updates.push(update);
      return task();
    },
    updateRun: async () => claim().run,
    heartbeat: async () => {},
  };
  let disposed = false;
  const sessionNames: string[] = [];
  const engine: AgentEngine = {
    createConversation: async (options) => {
      assert.equal(options.cwd, workspace);
      assert.equal(options.mode, "remote");
      if (!options.persisted) {
        // Throwaway session used to summarize the task title.
        return {
          sessionId: "title-session",
          prompt: async () => ({
            text: "Inspect tests",
            provider: "remote",
            model: "test-model",
            sessionId: "title-session",
          }),
          setSessionName: () => {},
          dispose: async () => {},
        };
      }
      return {
        sessionId: "pi-session",
        sessionFile: join(workspace, "pi-session.jsonl"),
        prompt: async (prompt) => {
          assert.match(prompt, /Inspect tests/u);
          return {
            text: "All tests pass",
            provider: "remote",
            model: "test-model",
            sessionId: "pi-session",
          };
        },
        setSessionName: (name) => {
          sessionNames.push(name);
        },
        dispose: async () => {
          disposed = true;
        },
      };
    },
  };

  const worker = new TaskWorker(config(workspace), hub, engine, () => {});
  assert.equal(await worker.runOnce(), true);
  assert.deepEqual(sessionNames, ["Inspect tests"]);
  assert.deepEqual(
    updates.map((update) => update.status),
    ["working", "completed"],
  );
  assert.deepEqual(updates[1]?.result, {
    text: "All tests pass",
    provider: "remote",
    model: "test-model",
    pi_session_id: "pi-session",
    pi_session_mode: "per_task",
    pi_session_reused: false,
    worker_slot: 0,
  });
  assert.equal(disposed, true);
});

test("continuous worker reuses one session and resumes it after restart", async () => {
  const workspace = temporaryDirectory();
  const continuousConfig: AgentHostConfig = {
    ...config(workspace),
    workerSessionMode: "continuous",
  };
  mkdirSync(continuousConfig.sessionDir, { recursive: true });
  const sessionFile = join(continuousConfig.sessionDir, "continuous.jsonl");
  writeFileSync(sessionFile, "{}\n");
  const claims = ["task-1", "task-2", "task-3"].map((taskId, index) => ({
    ...claim(),
    task: {
      ...task(),
      task_id: taskId,
      objective: `Objective ${index + 1}`,
    },
    run: {
      ...claim().run,
      run_id: `run-${index + 1}`,
      task_id: taskId,
      objective: `Objective ${index + 1}`,
    },
  }));
  const taskResults: Array<Record<string, unknown>> = [];
  const hub = {
    claimTask: async () => claims.shift() ?? null,
    updateTask: async (_taskId: string, update: Record<string, unknown>) => {
      if (update.status === "completed") taskResults.push(update.result as Record<string, unknown>);
      return task();
    },
    updateRun: async () => claim().run,
    heartbeat: async () => {},
  };
  const createOptions: Array<{
    persisted: boolean;
    sessionFile?: string;
  }> = [];
  const taskContexts: Array<string | undefined> = [];
  const sessionNames: string[] = [];
  let position = 0;
  let disposed = 0;
  const engine: AgentEngine = {
    createConversation: async (options) => {
      createOptions.push({
        persisted: options.persisted,
        ...(options.sessionFile ? { sessionFile: options.sessionFile } : {}),
      });
      return {
        sessionId: "continuous-session",
        sessionFile,
        prompt: async (prompt) => {
          assert.match(prompt, /BEGIN NEW REMOTE TASK/u);
          position += 2;
          return {
            text: `completed-${position}`,
            provider: "remote",
            model: "test-model",
            sessionId: "continuous-session",
          };
        },
        getSessionPosition: () => ({
          entryCount: position,
          messageCount: position,
        }),
        setTaskContext: (context) => taskContexts.push(context?.taskId),
        setSessionName: (name) => sessionNames.push(name),
        dispose: async () => {
          disposed += 1;
        },
      };
    },
  };

  const firstWorker = new TaskWorker(
    continuousConfig,
    hub,
    engine,
    () => {},
  );
  assert.equal(await firstWorker.runOnce(), true);
  assert.equal(await firstWorker.runOnce(), true);
  assert.equal(createOptions.length, 1);
  assert.equal(disposed, 0);
  await firstWorker.dispose();
  assert.equal(disposed, 1);

  const restartedWorker = new TaskWorker(
    continuousConfig,
    hub,
    engine,
    () => {},
  );
  assert.equal(await restartedWorker.runOnce(), true);
  assert.equal(createOptions.length, 2);
  assert.equal(createOptions[1]?.sessionFile, sessionFile);
  await restartedWorker.dispose();
  assert.equal(disposed, 2);

  assert.deepEqual(
    taskResults.map((result) => result.pi_session_reused),
    [false, true, true],
  );
  assert.deepEqual(
    taskResults.map((result) => result.pi_session_id),
    ["continuous-session", "continuous-session", "continuous-session"],
  );
  assert.deepEqual(
    taskResults.map((result) => [
      result.pi_turn_start_entry,
      result.pi_turn_end_entry,
    ]),
    [
      [0, 2],
      [2, 4],
      [4, 6],
    ],
  );
  assert.deepEqual(taskContexts, [
    "task-1",
    undefined,
    "task-2",
    undefined,
    "task-3",
    undefined,
  ]);
  assert.deepEqual(sessionNames, [`Worker 1 · ${basename(workspace)}`]);

  const registry = new RunSessionRegistry(continuousConfig.sessionDir);
  assert.equal(registry.get("run-1")?.sessionId, "continuous-session");
  assert.equal(registry.get("run-2")?.turnStartEntry, 2);
  assert.equal(registry.get("run-3")?.turnEndEntry, 6);
});

test("continuous worker rotates after its configured task limit", async () => {
  const workspace = temporaryDirectory();
  const rotatingConfig: AgentHostConfig = {
    ...config(workspace),
    workerSessionMode: "continuous",
    workerSessionMaxTasks: 1,
  };
  mkdirSync(rotatingConfig.sessionDir, { recursive: true });
  const claims = ["task-1", "task-2"].map((taskId, index) => ({
    ...claim(),
    task: { ...task(), task_id: taskId, objective: `Objective ${index + 1}` },
    run: {
      ...claim().run,
      run_id: `run-${index + 1}`,
      task_id: taskId,
      objective: `Objective ${index + 1}`,
    },
  }));
  const results: Array<Record<string, unknown>> = [];
  const hub = {
    claimTask: async () => claims.shift() ?? null,
    updateTask: async (_taskId: string, update: Record<string, unknown>) => {
      if (update.status === "completed") {
        results.push(update.result as Record<string, unknown>);
      }
      return task();
    },
    updateRun: async () => claim().run,
    heartbeat: async () => {},
  };
  let created = 0;
  let disposed = 0;
  const engine: AgentEngine = {
    createConversation: async () => {
      created += 1;
      const sessionId = `rotated-session-${created}`;
      const sessionFile = join(rotatingConfig.sessionDir, `${sessionId}.jsonl`);
      writeFileSync(sessionFile, "{}\n");
      return {
        sessionId,
        sessionFile,
        prompt: async () => ({
          text: "complete",
          provider: "remote",
          model: "test-model",
          sessionId,
        }),
        setSessionName: () => {},
        dispose: async () => {
          disposed += 1;
        },
      };
    },
  };

  const worker = new TaskWorker(rotatingConfig, hub, engine, () => {});
  assert.equal(await worker.runOnce(), true);
  assert.equal(await worker.runOnce(), true);
  await worker.dispose();

  assert.equal(created, 2);
  assert.equal(disposed, 2);
  assert.deepEqual(
    results.map((result) => [result.pi_session_id, result.pi_session_reused]),
    [
      ["rotated-session-1", false],
      ["rotated-session-2", false],
    ],
  );
});

test("self_update task runs without an LLM session and reports failure", async () => {
  const workspace = temporaryDirectory();
  const updates: Array<Record<string, unknown>> = [];
  const hub = {
    claimTask: async () => ({
      ...claim(),
      task: {
        ...task(),
        objective: "Self-update this agent",
        input: { workspace: ".", action: "self_update" },
      },
    }),
    updateTask: async (_taskId: string, update: Record<string, unknown>) => {
      updates.push(update);
      return task();
    },
    updateRun: async () => claim().run,
    heartbeat: async () => {},
  };
  let sessionsCreated = 0;
  const engine: AgentEngine = {
    createConversation: async () => {
      sessionsCreated += 1;
      throw new Error("self_update must not create a Pi session");
    },
  };
  let restarted = false;
  const worker = new TaskWorker(
    config(workspace),
    hub,
    engine,
    () => {},
    () => {
      restarted = true;
    },
  );

  // The temporary workspace is not a git repository, so the update fails
  // cleanly: task reported as failed, no restart, no LLM session.
  assert.equal(await worker.runOnce(), true);
  assert.equal(sessionsCreated, 0);
  assert.equal(restarted, false);
  assert.deepEqual(
    updates.map((update) => update.status),
    ["working", "failed"],
  );
  assert.match(
    String((updates[1]?.result as Record<string, unknown>)?.text ?? ""),
    /Self-update failed/u,
  );
});

test("task workspace cannot escape the configured root", () => {
  const workspace = temporaryDirectory();
  assert.throws(
    () => resolveTaskWorkspace(workspace, task("../outside")),
    /escapes AGENT_WORKSPACE_ROOT/u,
  );
});

test("worker fails an invalid workspace once instead of poisoning the queue", async () => {
  const workspace = temporaryDirectory();
  const updates: Array<Record<string, unknown>> = [];
  const invalidClaim = {
    ...claim(),
    task: task("missing-directory"),
  };
  const hub = {
    claimTask: async () => invalidClaim,
    updateTask: async (_taskId: string, update: Record<string, unknown>) => {
      updates.push(update);
      return task();
    },
    updateRun: async () => invalidClaim.run,
    heartbeat: async () => {},
  };
  let sessions = 0;
  const engine: AgentEngine = {
    createConversation: async () => {
      sessions += 1;
      throw new Error("invalid workspace must fail before creating a session");
    },
  };
  const worker = new TaskWorker(config(workspace), hub, engine, () => {});
  assert.equal(await worker.runOnce(), true);
  assert.equal(sessions, 0);
  assert.deepEqual(updates.map((update) => update.status), ["failed"]);
  assert.match(String(updates[0]?.message), /does not exist/u);
});

test("worker injects durable controls into the owning Pi session before ACK", async () => {
  const workspace = temporaryDirectory();
  const applied: string[] = [];
  const acknowledged: string[] = [];
  const hub = {
    claimTask: async () => null,
    updateTask: async () => task(),
    updateRun: async () => claim().run,
    heartbeat: async () => {},
    getTask: async () => task(),
    claimTaskControls: async () => [
      {
        seq: 1,
        control_id: "control-steer",
        task_id: "task-1",
        run_id: "run-1",
        kind: "steer" as const,
        message: "focus on tests",
        actor_id: "actor-owner",
        status: "leased" as const,
        lease_token: "control-lease-1",
        lease_until: Date.now() / 1_000 + 30,
        created_at: Date.now() / 1_000,
        delivered_at: null,
      },
      {
        seq: 2,
        control_id: "control-follow-up",
        task_id: "task-1",
        run_id: "run-1",
        kind: "follow_up" as const,
        message: "then summarize",
        actor_id: "actor-owner",
        status: "leased" as const,
        lease_token: "control-lease-2",
        lease_until: Date.now() / 1_000 + 30,
        created_at: Date.now() / 1_000,
        delivered_at: null,
      },
    ],
    acknowledgeTaskControl: async (_taskId: string, controlId: string) => {
      acknowledged.push(controlId);
    },
  };
  const engine: AgentEngine = {
    createConversation: async () => {
      throw new Error("not used");
    },
  };
  const conversation: AgentConversation = {
    sessionId: "pi-session",
    prompt: async () => {
      throw new Error("not used");
    },
    setSessionName: () => {},
    steer: async (message) => {
      applied.push(`steer:${message}`);
    },
    followUp: async (message) => {
      applied.push(`follow_up:${message}`);
    },
    dispose: async () => {},
  };
  const worker = new TaskWorker(config(workspace), hub, engine, () => {});
  const poll = worker as unknown as {
    pollTaskControls(
      taskId: string,
      runId: string,
      taskLeaseToken: string,
      current: AgentConversation,
    ): Promise<"active" | "cancelled">;
  };

  assert.equal(
    await poll.pollTaskControls("task-1", "run-1", "task-lease", conversation),
    "active",
  );
test("unsupported runtimes resolve queued controls with an explicit Hub status", async () => {
  const workspace = temporaryDirectory();
  const unsupported: Array<{ control_id: string; reason: string }> = [];
  const hub = {
    claimTask: async () => null,
    updateTask: async () => task(),
    updateRun: async () => claim().run,
    heartbeat: async () => {},
    getTask: async () => task(),
    claimTaskControls: async () => [
      {
        seq: 1,
        control_id: "control-steer",
        task_id: "task-1",
        run_id: "run-1",
        kind: "steer" as const,
        message: "focus on tests",
        actor_id: "actor-owner",
        status: "leased" as const,
        lease_token: "control-lease-1",
        lease_until: Date.now() / 1_000 + 30,
        created_at: Date.now() / 1_000,
        delivered_at: null,
      },
    ],
    markTaskControlUnsupported: async (
      _taskId: string,
      controlId: string,
      item: { reason: string },
    ) => {
      unsupported.push({ control_id: controlId, reason: item.reason });
    },
  };
  const engine: AgentEngine = {
    createConversation: async () => {
      throw new Error("not used");
    },
  };
  const worker = new TaskWorker(
    config(workspace),
    hub,
    engine,
    () => {},
    undefined,
    0,
    DSH_ENGINE_PROFILE,
  );
  const poll = worker as unknown as {
    pollTaskControls(
      taskId: string,
      runId: string,
      taskLeaseToken: string,
      current: AgentConversation,
    ): Promise<"active" | "cancelled">;
  };
  const conversation: AgentConversation = {
    sessionId: "dsh-session",
    prompt: async () => {
      throw new Error("not used");
    },
    setSessionName: () => {},
    dispose: async () => {},
  };

  assert.equal(
    await poll.pollTaskControls("task-1", "run-1", "task-lease", conversation),
    "active",
  );
  assert.equal(unsupported.length, 1);
  assert.equal(unsupported[0]?.control_id, "control-steer");
  assert.match(unsupported[0]?.reason ?? "", /does not support/u);
});
  assert.deepEqual(applied, [
    "steer:focus on tests",
    "follow_up:then summarize",
  ]);
  assert.deepEqual(acknowledged, ["control-steer", "control-follow-up"]);
});

test("remote model endpoint rejects loopback", () => {
  assert.throws(
    () => assertRemoteUrl("http://127.0.0.1:18080/v1"),
    /only accepts a remote model endpoint/u,
  );
  assert.equal(
    assertRemoteUrl("https://api.example/v1/"),
    "https://api.example/v1",
  );
});

test("Hub is optional but partial Hub configuration is rejected", () => {
  assert.deepEqual(resolveHubConfig(), { hubEnabled: false });
  assert.throws(
    () => resolveHubConfig("https://hub.test.invalid", undefined),
    /must be configured together/u,
  );
  assert.deepEqual(
    resolveHubConfig(
      "https://hub.test.invalid/",
      "test-hub-token-with-24-characters",
    ),
    {
      hubEnabled: true,
      hubUrl: "https://hub.test.invalid",
      hubToken: "test-hub-token-with-24-characters",
    },
  );
});

test("agent-specific environment takes precedence over the legacy project env", () => {
  const repository = temporaryDirectory();
  const host = join(repository, "agent-host");
  mkdirSync(host);
  writeFileSync(join(repository, ".env"), "SOURCE=legacy\n");
  writeFileSync(join(repository, ".env.agent"), "SOURCE=agent\n");
  assert.equal(discoverProjectEnv(repository), join(repository, ".env.agent"));
  assert.equal(discoverProjectEnv(host), join(repository, ".env.agent"));
  rmSync(join(repository, ".env.agent"));
  assert.equal(discoverProjectEnv(repository), join(repository, ".env"));
  assert.equal(discoverProjectEnv(host), join(repository, ".env"));
});

test("setup stores the model key outside its non-secret configuration", () => {
  const workspace = temporaryDirectory();
  const configPath = join(workspace, ".env.agent");
  const script = resolve(process.cwd(), "scripts/setup.mjs");
  const credential = temporaryCredentialNamespace();
  const modelEntry = trackedCredential(
    `${credential.namespace} Remote LLM API`,
    credential.account,
  );
  const result = spawnSync(process.execPath, [script, "--configure-only"], {
    cwd: workspace,
    env: {
      ...process.env,
      AGENT_SETUP_CONFIG_PATH: configPath,
      AGENT_SETUP_WORKSPACE: workspace,
      AGENT_CREDENTIAL_NAMESPACE: credential.namespace,
      AGENT_CREDENTIAL_ACCOUNT: credential.account,
      AGENT_HUB_URL: "",
      AGENT_HUB_TOKEN: "",
      AGENT_REMOTE_BASE_URL: "https://model.test.invalid/v1",
      AGENT_REMOTE_MODEL: "test-model",
      AGENT_REMOTE_API_KEY: "test-model-key",
    },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const contents = readFileSync(configPath, "utf8");
  assert.doesNotMatch(contents, /AGENT_HUB_/u);
  assert.doesNotMatch(contents, /test-model-key/u);
  assert.doesNotMatch(contents, /^AGENT_REMOTE_API_KEY=/mu);
  assert.match(contents, /AGENT_REMOTE_MODEL="test-model"/u);
  assert.ok(
    contents.includes(
      `AGENT_REMOTE_API_KEY_CREDENTIAL_SERVICE="${credential.namespace} Remote LLM API"`,
    ),
  );
  assert.equal(modelEntry.getPassword(), "test-model-key");
  assert.doesNotMatch(`${result.stdout}${result.stderr}`, /test-model-key/u);
  const repeated = spawnSync(process.execPath, [script, "--configure-only"], {
    cwd: workspace,
    env: {
      ...process.env,
      AGENT_SETUP_CONFIG_PATH: configPath,
      AGENT_SETUP_WORKSPACE: workspace,
      AGENT_CREDENTIAL_NAMESPACE: credential.namespace,
      AGENT_CREDENTIAL_ACCOUNT: credential.account,
      AGENT_HUB_URL: "",
      AGENT_HUB_TOKEN: "",
      AGENT_REMOTE_BASE_URL: "",
      AGENT_REMOTE_MODEL: "",
      AGENT_REMOTE_API_KEY: "",
    },
    encoding: "utf8",
  });
  assert.equal(repeated.status, 0, repeated.stderr);
  assert.doesNotMatch(
    readFileSync(configPath, "utf8"),
    /test-model-key|^AGENT_REMOTE_API_KEY=/mu,
  );
  assert.match(contents, new RegExp(`AGENT_WORKSPACE_ROOT="${workspace}"`, "u"));
  if (process.platform !== "win32") {
    assert.equal(statSync(configPath).mode & 0o777, 0o600);
  }
});

test("setup stores model and Hub secrets only in the system credential store", () => {
  const workspace = temporaryDirectory();
  const configPath = join(workspace, ".env.agent");
  const script = resolve(process.cwd(), "scripts/setup.mjs");
  const credential = temporaryCredentialNamespace();
  const modelEntry = trackedCredential(
    `${credential.namespace} Remote LLM API`,
    credential.account,
  );
  const hubEntry = trackedCredential(
    `${credential.namespace} Hub`,
    credential.account,
  );
  const result = spawnSync(process.execPath, [script, "--configure-only"], {
    cwd: workspace,
    env: {
      ...process.env,
      AGENT_SETUP_CONFIG_PATH: configPath,
      AGENT_SETUP_WORKSPACE: workspace,
      AGENT_CREDENTIAL_NAMESPACE: credential.namespace,
      AGENT_CREDENTIAL_ACCOUNT: credential.account,
      AGENT_HUB_URL: "https://hub.test.invalid",
      AGENT_HUB_TOKEN: "test-hub-token-with-24-characters",
      AGENT_REMOTE_BASE_URL: "https://model.test.invalid/v1",
      AGENT_REMOTE_MODEL: "test-model",
      AGENT_REMOTE_API_KEY: "test-model-key",
    },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const contents = readFileSync(configPath, "utf8");
  assert.match(contents, /AGENT_HUB_URL="https:\/\/hub\.test\.invalid"/u);
  assert.doesNotMatch(contents, /test-model-key|test-hub-token-with-24-characters/u);
  assert.doesNotMatch(contents, /^AGENT_(?:REMOTE_API_KEY|HUB_TOKEN)=/mu);
  assert.ok(
    contents.includes(
      `AGENT_HUB_TOKEN_CREDENTIAL_SERVICE="${credential.namespace} Hub"`,
    ),
  );
  assert.equal(modelEntry.getPassword(), "test-model-key");
  assert.equal(hubEntry.getPassword(), "test-hub-token-with-24-characters");
  assert.doesNotMatch(
    `${result.stdout}${result.stderr}`,
    /test-model-key|test-hub-token-with-24-characters/u,
  );
});

test("setup migrates legacy plaintext secrets out of an existing config", () => {
  const workspace = temporaryDirectory();
  const configPath = join(workspace, ".env.agent");
  const script = resolve(process.cwd(), "scripts/setup.mjs");
  const credential = temporaryCredentialNamespace();
  const modelEntry = trackedCredential(
    `${credential.namespace} Remote LLM API`,
    credential.account,
  );
  const hubEntry = trackedCredential(
    `${credential.namespace} Hub`,
    credential.account,
  );
  writeFileSync(
    configPath,
    [
      "AGENT_REMOTE_BASE_URL=https://model.test.invalid/v1",
      "AGENT_REMOTE_MODEL=test-model",
      "AGENT_REMOTE_API_KEY=legacy-model-secret",
      "AGENT_HUB_URL=https://hub.test.invalid",
      "AGENT_HUB_TOKEN=legacy-hub-token-with-24-characters",
      "",
    ].join("\n"),
  );
  const result = spawnSync(process.execPath, [script, "--configure-only"], {
    cwd: workspace,
    env: {
      ...process.env,
      AGENT_SETUP_CONFIG_PATH: configPath,
      AGENT_SETUP_WORKSPACE: workspace,
      AGENT_CREDENTIAL_NAMESPACE: credential.namespace,
      AGENT_CREDENTIAL_ACCOUNT: credential.account,
      AGENT_HUB_URL: "",
      AGENT_HUB_TOKEN: "",
      AGENT_REMOTE_BASE_URL: "",
      AGENT_REMOTE_MODEL: "",
      AGENT_REMOTE_API_KEY: "",
      LLM_API_KEY: "",
    },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const contents = readFileSync(configPath, "utf8");
  assert.doesNotMatch(contents, /legacy-model-secret|legacy-hub-token/u);
  assert.doesNotMatch(contents, /^AGENT_(?:REMOTE_API_KEY|HUB_TOKEN)=/mu);
  assert.equal(modelEntry.getPassword(), "legacy-model-secret");
  assert.equal(hubEntry.getPassword(), "legacy-hub-token-with-24-characters");
});

test("run registry resolves run, task, and session identifiers", () => {
  const workspace = temporaryDirectory();
  const registry = new RunSessionRegistry(workspace);
  registry.upsert({
    runId: "run-1",
    taskId: "task-1",
    sessionId: "session-1",
    sessionFile: join(workspace, "session-1.jsonl"),
    cwd: workspace,
    origin: "remote_task",
    status: "active",
  });
  assert.equal(registry.get("run-1")?.sessionId, "session-1");
  assert.equal(registry.get("task-1")?.runId, "run-1");
  assert.equal(registry.get("session-1")?.taskId, "task-1");
  assert.equal(registry.updateStatus("run-1", "completed")?.status, "completed");
});
