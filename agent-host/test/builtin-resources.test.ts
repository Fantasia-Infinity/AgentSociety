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
      mcpServers: Record<string, { directTools: boolean; lifecycle: string }>;
    };
    assert.equal(
      mcpConfig.mcpServers["agent-society-channel"]?.directTools,
      true,
    );
    assert.equal(
      mcpConfig.mcpServers["agent-society-channel"]?.lifecycle,
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
      const tools = session.getActiveToolNames();
      assert.ok(tools.includes("mcp"));
      assert.ok(tools.includes("lsp_hover"));
      assert.ok(tools.includes("channel_list_conversations"));
      assert.ok(tools.includes("channel_send"));
    } finally {
      await runtimeHost.dispose();
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
