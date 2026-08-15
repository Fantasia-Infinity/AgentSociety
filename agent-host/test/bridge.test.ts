import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import type { AgentHostConfig } from "../src/config.js";
import { HubClient } from "../src/hub-client.js";
import {
  BridgeWorker,
  discoverSessionId,
  parseStdoutResult,
  readAdapterResult,
  renderArgs,
  renderEnv,
  writeTaskEnvelope,
} from "../src/bridge.js";
import { AdapterSessionRegistry } from "../src/adapter-session-registry.js";
import { validateAdapterManifest } from "../src/adapter-registry.js";
import type { AdapterManifest } from "../src/bridge-types.js";
import type { HubClaim, HubTask } from "../src/types.js";

const temporaryDirectories: string[] = [];
afterEach(() => {
  for (const path of temporaryDirectories.splice(0)) {
    rmSync(path, { recursive: true, force: true });
  }
});

function temporaryDirectory(): string {
  const path = mkdtempSync(join(tmpdir(), "agent-bridge-test-"));
  temporaryDirectories.push(path);
  return path;
}

function config(workspaceRoot: string, overrides: Partial<AgentHostConfig> = {}): AgentHostConfig {
  return {
    hubEnabled: true,
    hubUrl: "http://127.0.0.1:8090",
    hubToken: "test-token",
    principalId: "principal-owner",
    principalDisplayName: "Owner",
    actorId: "actor-bridge",
    actorDisplayName: "Bridge",
    nodeId: "node-bridge",
    nodeDisplayName: "Bridge Node",
    workspaceRoot,
    sessionDir: join(workspaceRoot, ".sessions"),
    pollSeconds: 1,
    leaseSeconds: 30,
    workerConcurrency: 1,
    workerSupervised: false,
    workerSessionMode: "per_task",
    workerSessionMaxTasks: 0,
    workerSessionMaxAgeHours: 0,
    remoteToolPolicy: "read_only",
    remotePiResourcePolicy: "disabled",
    selfUpdateEnabled: false,
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
    ...overrides,
  };
}

function task(workspace = "."): HubTask {
  return {
    task_id: "task-1",
    context_id: null,
    principal_id: "principal-owner",
    delegator_actor_id: "actor-owner",
    assignee_actor_id: "actor-bridge",
    objective: "Write a report",
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
      actor_id: "actor-bridge",
      node_id: "node-bridge",
      origin: "remote_task",
      objective: "Write a report",
      status: "active",
      result: {},
      error: null,
    },
    lease_token: "lease-token",
  };
}

function manifest(overrides: Partial<AdapterManifest> = {}): AdapterManifest {
  return {
    id: "test",
    display_name: "Test Adapter",
    capabilities: ["code"],
    command: ["node", "missing.mjs"],
    args: ["{task_file}"],
    env: {},
    result_mode: "file",
    timeout_seconds: 60,
    cancel_grace_seconds: 1,
    ...overrides,
  };
}

function adapterScript(source: string): string {
  const workspace = temporaryDirectory();
  const path = join(workspace, "adapter.mjs");
  writeFileSync(path, source);
  return path;
}

const completingAdapter = `
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
const taskFile = process.env.AGENT_HUB_TASK_FILE;
const envelope = JSON.parse(readFileSync(taskFile, "utf8"));
const result = {
  status: "completed",
  text: "session=" + (process.env.AGENT_HUB_SESSION_ID || "none") + ":" + envelope.objective,
  session_id: envelope.session_id || "ses-1",
};
writeFileSync(join(dirname(taskFile), "AGENT_RESULT.json"), JSON.stringify(result));
`;

const failingAdapter = `process.exit(7);`;

const sleepingAdapter = `
import { writeFileSync } from "node:fs";
writeFileSync(process.argv[2], "started");
setTimeout(() => process.exit(0), 5000);
`;

test("renderArgs substitutes known placeholders", () => {
  assert.deepEqual(
    renderArgs(["--session", "{session_id}", "{prompt}"], {
      session_id: "ses-1",
      prompt: "hello",
      task_file: "/tmp/task.json",
      workspace: "/tmp",
      sandbox: "workspace-write",
    }),
    ["--session", "ses-1", "hello"],
  );
  assert.deepEqual(
    renderArgs(["--sandbox", "{sandbox}", "{prompt}"], {
      session_id: "",
      prompt: "hello",
      task_file: "/tmp/task.json",
      workspace: "/tmp",
      sandbox: "read-only",
    }),
    ["--sandbox", "read-only", "hello"],
  );
});

test("validateAdapterManifest accepts a valid manifest and rejects bad ones", () => {
  const valid = validateAdapterManifest({
    id: "sample",
    display_name: "Sample",
    capabilities: ["code"],
    command: ["sample", "run"],
    args: ["{prompt}"],
    result_mode: "stdout_json",
    session: {
      resume: true,
      new_args: ["{prompt}"],
      resume_args: ["--session", "{session_id}", "{prompt}"],
    },
  });
  assert.equal(valid.id, "sample");
  assert.throws(
    () =>
      validateAdapterManifest({
        id: "sample",
        display_name: "Sample",
        capabilities: [],
        command: ["sample"],
        args: ["{unknown}"],
        result_mode: "stdout_json",
      }),
    /Unknown placeholder/,
  );
  assert.doesNotThrow(() =>
    validateAdapterManifest({
      id: "sample",
      display_name: "Sample",
      capabilities: [],
      command: ["sample"],
      args: ["--sandbox", "{sandbox}"],
      result_mode: "stdout_json",
    }),
  );
  assert.throws(
    () =>
      validateAdapterManifest({
        id: "sample",
        display_name: "Sample",
        capabilities: [],
        command: ["sample"],
        args: [],
        result_mode: "stdout_json",
        session: { resume: true },
      }),
    /resume_args is required/,
  );
});

test("task envelope and result file round trip", () => {
  const workspace = temporaryDirectory();
  const envelopePath = writeTaskEnvelope(workspace, {
    task_id: "task-1",
    run_id: "run-1",
    objective: "objective",
    input: { workspace: "." },
    workspace,
    capabilities: [],
    session_id: "ses-1",
    continue: true,
  });
  assert.ok(existsSync(envelopePath));
  assert.deepEqual(JSON.parse(readFileSync(envelopePath, "utf8")), {
    task_id: "task-1",
    run_id: "run-1",
    objective: "objective",
    input: { workspace: "." },
    workspace,
    capabilities: [],
    session_id: "ses-1",
    continue: true,
  });

  const resultPath = join(workspace, "AGENT_RESULT.json");
  writeFileSync(
    resultPath,
    JSON.stringify({ status: "completed", text: "ok", session_id: "ses-2" }),
  );
  assert.deepEqual(readAdapterResult(resultPath), {
    status: "completed",
    text: "ok",
    session_id: "ses-2",
  });
  assert.equal(readAdapterResult(join(workspace, "missing.json")), undefined);
});

test("parseStdoutResult parses JSON and falls back to text", () => {
  assert.deepEqual(parseStdoutResult('{"status":"completed","text":"ok"}\n'), {
    status: "completed",
    text: "ok",
  });
  assert.equal(parseStdoutResult("plain text"), undefined);
});

test("parseStdoutResult extracts Codex JSONL events", () => {
  const stdout = [
    '{"type":"thread.started","thread_id":"019fcc11-c175-7540-ad53-41b9cae47e62"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"bridge-ok-1"}}',
    '{"type":"turn.completed","usage":{"output_tokens":39}}',
  ].join("\n");
  assert.deepEqual(parseStdoutResult(stdout), {
    text: "bridge-ok-1",
    session_id: "019fcc11-c175-7540-ad53-41b9cae47e62",
  });
});

test("parseStdoutResult extracts session metadata from desktop session events", () => {
  const stdout = [
    '{"type":"session_meta","payload":{"session_id":"019fc96b-01c5-7e50-ab12-3ca4b711bbfc"}}',
    '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"final answer"}]}}',
  ].join("\n");
  assert.deepEqual(parseStdoutResult(stdout), {
    text: "final answer",
    session_id: "019fc96b-01c5-7e50-ab12-3ca4b711bbfc",
  });
});

test("parseStdoutResult extracts OpenCode JSONL events", () => {
  const stdout = [
    '{"type":"step_start","sessionID":"ses_0339da3a5ffeF44DHwAOHkQqcH","part":{"type":"step-start"}}',
    '{"type":"text","sessionID":"ses_0339da3a5ffeF44DHwAOHkQqcH","part":{"type":"text","text":"opencode-ok-1"}}',
    '{"type":"step_finish","sessionID":"ses_0339da3a5ffeF44DHwAOHkQqcH","part":{"type":"step-finish","reason":"stop"}}',
  ].join("\n");
  assert.deepEqual(parseStdoutResult(stdout), {
    text: "opencode-ok-1",
    session_id: "ses_0339da3a5ffeF44DHwAOHkQqcH",
  });
});

test("adapter session registry keeps, resets, and rotates sessions", () => {
  const workspace = temporaryDirectory();
  const registry = new AdapterSessionRegistry(join(workspace, ".sessions"));
  const scope = {
    adapterId: "codex",
    actorId: "actor",
    nodeId: "node",
    principalId: "principal",
    workerSlot: 0,
    cwd: workspace,
  };
  registry.upsert(scope, "ses-1", "task-1");
  registry.upsert(scope, "ses-1", "task-2");
  assert.equal(registry.get(scope)?.taskCount, 2);
  registry.upsert(scope, "ses-2", "task-3", { reset: true });
  assert.equal(registry.get(scope)?.taskCount, 1);
  assert.equal(registry.get(scope)?.sessionId, "ses-2");
  registry.clear(scope);
  assert.equal(registry.get(scope), undefined);
});

test("discoverSessionId finds the newest session file", () => {
  const workspace = temporaryDirectory();
  const directory = join(workspace, ".opencode", "sessions");
  mkdirSync(directory, { recursive: true });
  const oldPath = join(directory, "ses_old.jsonl");
  const newPath = join(directory, "ses_new.jsonl");
  writeFileSync(oldPath, "old");
  writeFileSync(newPath, "new");
  const now = Date.now() / 1000;
  utimesSync(oldPath, now - 100, now - 100);
  utimesSync(newPath, now, now);
  assert.equal(
    discoverSessionId(workspace, ".opencode/sessions/*.jsonl"),
    "ses_new",
  );
});

test("discoverSessionId extracts UUID from nested rollout session files", () => {
  const workspace = temporaryDirectory();
  const directory = join(workspace, "sessions", "2026", "08", "03");
  mkdirSync(directory, { recursive: true });
  const path = join(
    directory,
    "rollout-2026-08-03T22-57-41-019fc96b-01c5-7e50-ab12-3ca4b711bbfc.jsonl",
  );
  writeFileSync(path, "{}");
  assert.equal(
    discoverSessionId(workspace, "sessions/**/*.jsonl"),
    "019fc96b-01c5-7e50-ab12-3ca4b711bbfc",
  );
});

test("bridge completes a claimed task through a CLI adapter", async () => {
  const workspace = temporaryDirectory();
  const script = adapterScript(completingAdapter);
  const updates: Array<Record<string, unknown>> = [];
  const hub = {
    claimTask: async () => claim(),
    updateTask: async (_taskId: string, update: Record<string, unknown>) => {
      updates.push(update);
      return update;
    },
    updateRun: async () => ({}),
    heartbeat: async () => {},
    getTask: async () => task(),
    addArtifact: async () => ({}),
  } as unknown as HubClient;
  const worker = new BridgeWorker(
    config(workspace),
    hub,
    manifest({ command: ["node", script] }),
  );
  try {
    assert.equal(await worker.runOnce(), true);
  } finally {
    await worker.dispose();
  }
  const completed = updates.find((update) => update.status === "completed");
  assert.ok(completed);
  assert.match(String((completed.result as { text?: string }).text), /session=none/);
  assert.ok(
    existsSync(join(workspace, ".agenthub", "run-1", "AGENT_TASK.json")),
  );
});

test("bridge resumes a continuous adapter session", async () => {
  const workspace = temporaryDirectory();
  const script = adapterScript(completingAdapter);
  const updates: Array<Record<string, unknown>> = [];
  const hub = {
    claimTask: async () => claim(),
    updateTask: async (_taskId: string, update: Record<string, unknown>) => {
      updates.push(update);
      return update;
    },
    updateRun: async () => ({}),
    heartbeat: async () => {},
    getTask: async () => task(),
    addArtifact: async () => ({}),
  } as unknown as HubClient;
  const bridgeConfig = config(workspace, { workerSessionMode: "continuous" });
  const adapter = manifest({
    command: ["node", script],
    session: {
      resume: true,
      new_args: ["{task_file}"],
      resume_args: ["{session_id}", "{task_file}"],
      result_field: "session_id",
    },
  });
  const worker = new BridgeWorker(bridgeConfig, hub, adapter);
  try {
    await worker.runOnce();
    await worker.runOnce();
  } finally {
    await worker.dispose();
  }
  const completed = updates.filter((update) => update.status === "completed");
  assert.equal(completed.length, 2);
  assert.match(
    String((completed[0]!.result as { text?: string }).text),
    /session=none/,
  );
  assert.match(
    String((completed[1]!.result as { text?: string }).text),
    /session=ses-1/,
  );
});

test("bridge cancels an adapter process when the Hub task is cancelled", async () => {
  const workspace = temporaryDirectory();
  const marker = join(workspace, "started.marker");
  const script = adapterScript(sleepingAdapter);
  const runUpdates: Array<Record<string, unknown>> = [];
  const hub = {
    claimTask: async () => claim(),
    updateTask: async (_taskId: string, update: Record<string, unknown>) => update,
    updateRun: async (_runId: string, update: Record<string, unknown>) => {
      runUpdates.push(update);
      return update;
    },
    heartbeat: async () => {},
    getTask: async () => ({ ...task(), status: "cancelled" }),
    addArtifact: async () => ({}),
  } as unknown as HubClient;
  const worker = new BridgeWorker(
    config(workspace),
    hub,
    manifest({
      command: ["node", script, marker],
      cancel_grace_seconds: 0,
      timeout_seconds: 30,
    }),
  );
  try {
    await worker.runOnce();
  } finally {
    await worker.dispose();
  }
  assert.ok(existsSync(marker), "adapter should have started");
  assert.equal(runUpdates.at(-1)?.status, "cancelled");
});

test("bridge fails the task on non-zero exit", async () => {
  const workspace = temporaryDirectory();
  const script = adapterScript(failingAdapter);
  const updates: Array<Record<string, unknown>> = [];
  const hub = {
    claimTask: async () => claim(),
    updateTask: async (_taskId: string, update: Record<string, unknown>) => {
      updates.push(update);
      return update;
    },
    updateRun: async () => ({}),
    heartbeat: async () => {},
    getTask: async () => task(),
    addArtifact: async () => ({}),
  } as unknown as HubClient;
  const worker = new BridgeWorker(
    config(workspace),
    hub,
    manifest({ command: ["node", script] }),
  );
  try {
    await assert.rejects(worker.runOnce(), /exited with code 7/);
  } finally {
    await worker.dispose();
  }
  assert.equal(updates.at(-1)?.status, "failed");
});

test("bridge fails the task on timeout", async () => {
  const workspace = temporaryDirectory();
  const marker = join(workspace, "started.marker");
  const script = adapterScript(sleepingAdapter);
  const updates: Array<Record<string, unknown>> = [];
  const hub = {
    claimTask: async () => claim(),
    updateTask: async (_taskId: string, update: Record<string, unknown>) => {
      updates.push(update);
      return update;
    },
    updateRun: async () => ({}),
    heartbeat: async () => {},
    getTask: async () => task(),
    addArtifact: async () => ({}),
  } as unknown as HubClient;
  const worker = new BridgeWorker(
    config(workspace),
    hub,
    manifest({
      command: ["node", script, marker],
      cancel_grace_seconds: 0,
      timeout_seconds: 1,
    }),
  );
  try {
    await assert.rejects(worker.runOnce(), /timed out/);
  } finally {
    await worker.dispose();
  }
  assert.equal(updates.at(-1)?.status, "failed");
});

test("bridge env placeholders resolve model and Hub credentials at spawn time", () => {
  const env = renderEnv(
    {
      DEEPSEEK_API_KEY: "{remote_api_key}",
      DEEPSEEK_BASE_URL: "{remote_base_url}",
      DSH_MODEL: "{remote_model}",
      AGENT_HUB_URL: "{hub_url}",
      AGENT_HUB_TOKEN: "{hub_token}",
    },
    {
      remote_api_key: "sk-test",
      remote_base_url: "https://models.example/v1",
      remote_model: "deepseek-v4-flash",
      hub_url: "http://127.0.0.1:8090",
      hub_token: "token",
    },
  );
  assert.deepEqual(env, {
    DEEPSEEK_API_KEY: "sk-test",
    DEEPSEEK_BASE_URL: "https://models.example/v1",
    DSH_MODEL: "deepseek-v4-flash",
    AGENT_HUB_URL: "http://127.0.0.1:8090",
    AGENT_HUB_TOKEN: "token",
  });
});

test("dsh adapter manifest is valid", () => {
  const value = JSON.parse(
    readFileSync(join(process.cwd(), "adapters", "dsh.json"), "utf8"),
  );
  assert.equal(validateAdapterManifest(value).id, "dsh");
});
