import assert from "node:assert/strict";
import { existsSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, test } from "node:test";

import { DshAgentEngine } from "../src/dsh-engine.js";
import { runDshDoctor } from "../src/dsh-doctor.js";
import type { AgentHostConfig } from "../src/config.js";

const temporaryDirectories: string[] = [];
afterEach(() => {
  for (const path of temporaryDirectories.splice(0)) {
    rmSync(path, { recursive: true, force: true });
  }
});

function temporaryDirectory(): string {
  const path = mkdtempSync(join(tmpdir(), "agent-host-dsh-test-"));
  temporaryDirectories.push(path);
  return path;
}

function fakeRuntimePath(): string {
  return resolve(process.cwd(), "test", "fixtures", "fake-dsh-runtime.mjs");
}

function config(workspaceRoot: string): AgentHostConfig {
  return {
    hubEnabled: false,
    principalId: "principal-owner",
    principalDisplayName: "Owner",
    actorId: "dsh-mac",
    actorDisplayName: "DeepSeek Harness on Mac",
    nodeId: "dsh-node",
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
    remoteToolPolicy: "full",
    remotePiResourcePolicy: "disabled",
    selfUpdateEnabled: false,
    builtinCapabilitiesEnabled: false,
    subagentMaxDepth: 2,
    subagentConcurrency: 4,
    backgroundMaxProcesses: 8,
    webSearchMode: "disabled",
    webSearchModel: "deepseek-v4-flash",
    remoteBaseUrl: "https://models.example/v1",
    remoteApiKey: "test-key",
    remoteModel: "deepseek-v4-flash",
    contextWindow: 128_000,
    maxOutputTokens: 8_192,
    thinkingLevel: "high",
    dshRuntimeBin: process.execPath,
    dshRuntimeArgs: [fakeRuntimePath()],
    dshConfigPath: join(workspaceRoot, "dsh-worker.cordis.yml"),
    dshModel: "deepseek-v4-flash",
    dshProvider: "deepseek-official",
    dshSessionRoot: join(workspaceRoot, "dsh-sessions"),
    dshPermissionMode: "workspace-write",
    dshMaxTokens: 8_192,
  };
}

test("DshAgentEngine drives a dsh runtime and streams assistant text", async () => {
  const workspace = temporaryDirectory();
  mkdirSync(join(workspace, "dsh-sessions"), { recursive: true });
  writeFileSync(
    join(workspace, "dsh-worker.cordis.yml"),
    "# fake config for the fake runtime\n",
  );
  const engine = await DshAgentEngine.create(config(workspace));
  const conversation = await engine.createConversation({
    cwd: workspace,
    mode: "remote",
    persisted: true,
  });
  const deltas: string[] = [];
  const result = await conversation.prompt("hello", (delta) => {
    deltas.push(delta);
  });
  assert.equal(result.text, "MOCK_DSH_ENGINE");
  assert.equal(result.provider, "deepseek-official");
  assert.equal(result.model, "deepseek-v4-flash");
  assert.deepEqual(deltas, ["MOCK_DSH_", "ENGINE"]);
  assert.ok(conversation.sessionFile);
  const position = conversation.getSessionPosition?.();
  assert.ok(position && position.entryCount > 0);
  await conversation.dispose();
  await engine.dispose();
});

test("DshAgentEngine rejects cross-process session resume", async () => {
  const workspace = temporaryDirectory();
  writeFileSync(
    join(workspace, "dsh-worker.cordis.yml"),
    "# fake config for the fake runtime\n",
  );
  const engine = await DshAgentEngine.create(config(workspace));
  await assert.rejects(
    engine.createConversation({
      cwd: workspace,
      mode: "remote",
      persisted: true,
      sessionFile: join(workspace, "old-session.jsonl"),
    }),
    /cannot be resumed across process restarts/u,
  );
  await engine.dispose();
});

test("DshAgentEngine abort invalidates the conversation and rejects the prompt", async () => {
  const workspace = temporaryDirectory();
  writeFileSync(
    join(workspace, "dsh-worker.cordis.yml"),
    "# fake config for the fake runtime\n",
  );
  const runtimeConfig = config(workspace);
  const previous = process.env.FAKE_DSH_STALL;
  const previousMarker = process.env.FAKE_DSH_STALL_MARKER;
  const marker = join(workspace, "stalled.marker");
  process.env.FAKE_DSH_STALL = "1";
  process.env.FAKE_DSH_STALL_MARKER = marker;
  try {
    const engine = await DshAgentEngine.create(runtimeConfig);
    const conversation = await engine.createConversation({
      cwd: workspace,
      mode: "remote",
      persisted: true,
    });
    const prompt = conversation.prompt("stall");
    for (let attempt = 0; attempt < 100 && !existsSync(marker); attempt += 1) {
      await new Promise((done) => setTimeout(done, 10));
    }
    assert.ok(existsSync(marker));
    await conversation.abort?.();
    await assert.rejects(prompt, /runtime was aborted/u);
    assert.equal(conversation.isUsable, false);
    await conversation.dispose();
    await engine.dispose();
  } finally {
    if (previous === undefined) delete process.env.FAKE_DSH_STALL;
    else process.env.FAKE_DSH_STALL = previous;
    if (previousMarker === undefined) delete process.env.FAKE_DSH_STALL_MARKER;
    else process.env.FAKE_DSH_STALL_MARKER = previousMarker;
  }
});
test("dsh doctor runs a diagnostic prompt through the dsh runtime", async () => {
  const workspace = temporaryDirectory();
  mkdirSync(workspace, { recursive: true });
  writeFileSync(
    join(workspace, "dsh-worker.cordis.yml"),
    "# fake config for the fake runtime\n",
  );
  await runDshDoctor(config(workspace));
});
