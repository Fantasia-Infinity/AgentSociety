import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, test } from "node:test";

import {
  assertRemoteUrl,
  discoverProjectEnv,
  type AgentHostConfig,
} from "../src/config.js";
import { RunSessionRegistry } from "../src/run-registry.js";
import type { AgentEngine, HubClaim, HubTask } from "../src/types.js";
import { resolveTaskWorkspace, TaskWorker } from "../src/worker.js";

const temporaryDirectories: string[] = [];
afterEach(() => {
  for (const path of temporaryDirectories.splice(0)) {
    rmSync(path, { recursive: true, force: true });
  }
});

function temporaryDirectory(): string {
  const path = mkdtempSync(join(tmpdir(), "agent-host-test-"));
  temporaryDirectories.push(path);
  return path;
}

function config(workspaceRoot: string): AgentHostConfig {
  return {
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
    remoteToolPolicy: "read_only",
    remoteBaseUrl: "https://models.example/v1",
    remoteApiKey: "key",
    remoteModel: "model",
    contextWindow: 100_000,
    maxOutputTokens: 4_096,
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
  const engine: AgentEngine = {
    createConversation: async (options) => {
      assert.equal(options.cwd, workspace);
      assert.equal(options.mode, "remote");
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
        dispose: () => {
          disposed = true;
        },
      };
    },
  };

  const worker = new TaskWorker(config(workspace), hub, engine, () => {});
  assert.equal(await worker.runOnce(), true);
  assert.deepEqual(
    updates.map((update) => update.status),
    ["working", "completed"],
  );
  assert.deepEqual(updates[1]?.result, {
    text: "All tests pass",
    provider: "remote",
    model: "test-model",
    pi_session_id: "pi-session",
  });
  assert.equal(disposed, true);
});

test("task workspace cannot escape the configured root", () => {
  const workspace = temporaryDirectory();
  assert.throws(
    () => resolveTaskWorkspace(workspace, task("../outside")),
    /escapes AGENT_WORKSPACE_ROOT/u,
  );
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

test("agent-specific environment takes precedence over the legacy project env", () => {
  const repository = temporaryDirectory();
  const host = join(repository, "agent-host");
  mkdirSync(host);
  writeFileSync(join(repository, ".env"), "SOURCE=legacy\n");
  writeFileSync(join(repository, ".env.agent"), "SOURCE=agent\n");
  assert.equal(discoverProjectEnv(host), join(repository, ".env.agent"));
  rmSync(join(repository, ".env.agent"));
  assert.equal(discoverProjectEnv(host), join(repository, ".env"));
});

test("setup writes the five supplied connections and automatic workspace", () => {
  const workspace = temporaryDirectory();
  const configPath = join(workspace, ".env.agent");
  const script = resolve(process.cwd(), "scripts/setup.mjs");
  const result = spawnSync(process.execPath, [script, "--configure-only"], {
    cwd: workspace,
    env: {
      ...process.env,
      AGENT_SETUP_CONFIG_PATH: configPath,
      AGENT_SETUP_WORKSPACE: workspace,
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
  assert.match(contents, /AGENT_REMOTE_MODEL="test-model"/u);
  assert.match(contents, new RegExp(`AGENT_WORKSPACE_ROOT="${workspace}"`, "u"));
  if (process.platform !== "win32") {
    assert.equal(statSync(configPath).mode & 0o777, 0o600);
  }
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
