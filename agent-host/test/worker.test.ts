import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import { assertRemoteUrl, type AgentHostConfig } from "../src/config.js";
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
    hubUrl: "http://127.0.0.1:8080",
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
    heartbeat: async () => {},
  };
  let disposed = false;
  const engine: AgentEngine = {
    createConversation: async (options) => {
      assert.equal(options.cwd, workspace);
      assert.equal(options.mode, "remote");
      return {
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
    session_id: "pi-session",
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
