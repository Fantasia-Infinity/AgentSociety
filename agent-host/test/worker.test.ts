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
import { join, resolve } from "node:path";
import { afterEach, test } from "node:test";

import {
  assertRemoteUrl,
  discoverProjectEnv,
  resolveHubConfig,
  type AgentHostConfig,
} from "../src/config.js";
import { RunSessionRegistry } from "../src/run-registry.js";
import type { AgentEngine, HubClaim, HubTask } from "../src/types.js";
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
  assert.equal(discoverProjectEnv(host), join(repository, ".env.agent"));
  rmSync(join(repository, ".env.agent"));
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
