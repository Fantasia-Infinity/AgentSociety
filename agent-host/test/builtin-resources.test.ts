import assert from "node:assert/strict";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  createAgentSessionFromServices,
  AgentSessionRuntime,
  ModelRuntime,
  SessionManager,
} from "@earendil-works/pi-coding-agent";

import { ensureBuiltinResourceDefaults } from "../src/builtin-resources.js";
import type { AgentHostConfig } from "../src/config.js";
import { PiAgentEngine } from "../src/pi-engine.js";
import {
  activateCompatibleTools,
  collectPiDiagnostics,
  createPiServices,
} from "../src/pi-compatibility.js";

test("managed MCP and LSP defaults load in a remote session", async () => {
  const root = mkdtempSync(join(tmpdir(), "builtin-resources-test-"));
  const workspace = join(root, "workspace");
  mkdirSync(workspace);
  const previousHome = process.env.HOME;
  const previousDb = process.env.AGENT_CHANNEL_STATE_DB;
  process.env.HOME = root;
  process.env.AGENT_CHANNEL_STATE_DB = join(root, "channel.sqlite3");
  try {
    const builtins = ensureBuiltinResourceDefaults();
    assert.deepEqual(builtins.diagnostics, []);
    const mcpConfigPath = join(root, ".pi", "agent", "mcp.json");
    const lspConfigPath = join(root, ".pi", "agent", "lsp.json");
    assert.ok(existsSync(mcpConfigPath));
    assert.ok(existsSync(lspConfigPath));
    const mcpConfig = JSON.parse(readFileSync(mcpConfigPath, "utf8")) as {
      mcpServers: Record<
        string,
        { command: string; directTools: boolean; lifecycle: string }
      >;
    };
    const channel = mcpConfig.mcpServers["agent-society-channel"];
    assert.ok(channel);
    assert.ok(channel.command.startsWith("/"));
    assert.equal(
      channel.directTools,
      true,
    );
    assert.equal(
      channel.lifecycle,
      "eager",
    );

    const runtime = await fixtureModelRuntime();
    const services = await createPiServices({
      cwd: workspace,
      agentDir: join(root, ".pi", "agent"),
      modelRuntime: runtime,
      mode: "remote",
      remotePiResourcePolicy: "disabled",
      builtinExtensionPaths: builtins.extensionPaths,
    });
    assert.deepEqual(
      collectPiDiagnostics(services).filter((item) => item.type === "error"),
      [],
    );
    const model = runtime.getModel("fixture", "fixture-model");
    assert.ok(model);
    const created = await createAgentSessionFromServices({
      services,
      model,
      sessionManager: SessionManager.inMemory(workspace),
      sessionStartEvent: { type: "session_start", reason: "startup" },
    });
    const { session } = created;
    const runtimeHost = new AgentSessionRuntime(
      session,
      services,
      async () => {
        throw new Error("fixture runtime does not replace sessions");
      },
    );
    try {
      activateCompatibleTools(session, "remote", "full");
      let tools = session.getActiveToolNames();
      assert.ok(tools.includes("mcp"));
      assert.ok(tools.includes("lsp_hover"));
      let channelReady = false;
      for (let attempt = 0; attempt < 40; attempt += 1) {
        if (
          tools.includes("channel_list_conversations") &&
          tools.includes("channel_send")
        ) {
          channelReady = true;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
        activateCompatibleTools(session, "remote", "full");
        tools = session.getActiveToolNames();
      }
      assert.ok(
        channelReady,
        `channel tools missing; active=${tools.join(",")}`,
      );
    } finally {
      await runtimeHost.dispose();
    }

    const config: AgentHostConfig = {
      hubEnabled: false,
      principalId: "principal-fixture",
      principalDisplayName: "Fixture",
      actorId: "actor-fixture",
      actorDisplayName: "Fixture Agent",
      nodeId: "node-fixture",
      nodeDisplayName: "Fixture Node",
      workspaceRoot: workspace,
      sessionDir: join(root, "sessions"),
      pollSeconds: 1,
      leaseSeconds: 30,
      workerConcurrency: 1,
      workerSupervised: false,
      workerSessionMode: "continuous",
      workerSessionMaxTasks: 0,
      workerSessionMaxAgeHours: 0,
      remoteToolPolicy: "full",
      remotePiResourcePolicy: "disabled",
      selfUpdateEnabled: false,
      builtinCapabilitiesEnabled: true,
      subagentMaxDepth: 2,
      subagentConcurrency: 2,
      backgroundMaxProcesses: 2,
      webSearchMode: "disabled",
      webSearchModel: "deepseek-v4-flash",
      remoteBaseUrl: "https://models.test.invalid/v1",
      remoteApiKey: "fixture-key",
      remoteModel: "fixture-model",
      contextWindow: 8_192,
      maxOutputTokens: 1_024,
      thinkingLevel: "off",
    };
    const engine = await PiAgentEngine.create(config);
    const empty = await engine.createConversation({
      cwd: workspace,
      mode: "remote",
      persisted: true,
    });
    const emptySessionFile = empty.sessionFile;
    assert.ok(emptySessionFile);
    empty.setTaskContext?.({ taskId: "task-empty", runId: "run-empty" });
    empty.setTaskContext?.();
    await empty.dispose();
    // Pi intentionally leaves a session lazy until an assistant message exists.
    assert.equal(existsSync(emptySessionFile), false);

    const seeded = SessionManager.create(workspace, config.sessionDir);
    seeded.appendMessage({
      role: "user",
      content: "fixture request",
      timestamp: Date.now(),
    });
    seeded.appendMessage({
      role: "assistant",
      content: [{ type: "text", text: "fixture response" }],
      api: "openai-completions",
      provider: "fixture",
      model: "fixture-model",
      usage: {
        input: 1,
        output: 1,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 2,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      },
      stopReason: "stop",
      timestamp: Date.now(),
    });
    const firstSessionId = seeded.getSessionId();
    const sessionFile = seeded.getSessionFile();
    assert.ok(sessionFile);
    assert.ok(existsSync(sessionFile));
    const resumed = await engine.createConversation({
      cwd: workspace,
      mode: "remote",
      persisted: true,
      sessionFile,
    });
    try {
      assert.equal(resumed.sessionId, firstSessionId);
      assert.equal(resumed.sessionFile, sessionFile);
      resumed.setSessionName("Continuous fixture");
      resumed.setTaskContext?.({ taskId: "task-fixture", runId: "run-fixture" });
      resumed.setTaskContext?.();
    } finally {
      await resumed.dispose();
    }
  } finally {
    if (previousHome === undefined) delete process.env.HOME;
    else process.env.HOME = previousHome;
    if (previousDb === undefined) delete process.env.AGENT_CHANNEL_STATE_DB;
    else process.env.AGENT_CHANNEL_STATE_DB = previousDb;
    rmSync(root, { recursive: true, force: true });
  }
});

async function fixtureModelRuntime(): Promise<ModelRuntime> {
  const runtime = await ModelRuntime.create();
  runtime.registerProvider("fixture", {
    name: "Fixture",
    baseUrl: "https://models.test.invalid/v1",
    apiKey: "fixture-key",
    api: "openai-completions",
    models: [
      {
        id: "fixture-model",
        name: "Fixture model",
        api: "openai-completions",
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 8_192,
        maxTokens: 1_024,
      },
    ],
  });
  return runtime;
}
