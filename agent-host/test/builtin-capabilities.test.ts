import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import { createBuiltinCapabilityBundle } from "../src/builtin-capabilities.js";
import type { AgentConversation } from "../src/types.js";

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const path of temporaryDirectories.splice(0)) {
    rmSync(path, { recursive: true, force: true });
  }
});

function temporaryDirectory(): string {
  const path = mkdtempSync(join(tmpdir(), "builtin-capabilities-test-"));
  temporaryDirectories.push(path);
  return path;
}

type ExecutableTool = {
  name: string;
  execute(
    id: string,
    params: unknown,
    signal: AbortSignal,
  ): Promise<{ details: unknown }>;
};

async function callTool(
  tools: readonly unknown[],
  name: string,
  params: unknown,
): Promise<unknown> {
  const tool = tools.find(
    (candidate) =>
      typeof candidate === "object" &&
      candidate !== null &&
      "name" in candidate &&
      candidate.name === name,
  ) as ExecutableTool | undefined;
  assert.ok(tool, `missing tool ${name}`);
  return (await tool.execute("test-call", params, new AbortController().signal))
    .details;
}

function fakeConversation(objectiveLog: string[]): AgentConversation {
  return {
    sessionId: "child-session",
    setSessionName: () => undefined,
    prompt: async (objective) => {
      objectiveLog.push(objective);
      return {
        text: "child result",
        provider: "fixture",
        model: "fixture",
        sessionId: "child-session",
      };
    },
    dispose: async () => undefined,
  };
}

test("built-in tools persist plan and scoped memory and run sub-agents", async () => {
  const root = temporaryDirectory();
  const workspace = join(root, "workspace");
  mkdirSync(workspace);
  const objectives: string[] = [];
  const bundle = createBuiltinCapabilityBundle({
    cwd: workspace,
    mode: "remote",
    sessionId: "session-1",
    sessionDir: join(root, "sessions"),
    principalId: "principal-1",
    subagentDepth: 0,
    subagentMaxDepth: 2,
    subagentConcurrency: 4,
    backgroundMaxProcesses: 2,
    createSubagent: async () => fakeConversation(objectives),
  });
  try {
    const plan = (await callTool(bundle.tools, "plan_set", {
      title: "Ship capability bundle",
      steps: [{ text: "Implement", status: "in_progress" }],
    })) as { steps: Array<{ id: string; status: string }> };
    assert.equal(plan.steps[0]?.status, "in_progress");
    const restored = (await callTool(bundle.tools, "plan_get", {})) as {
      title: string;
    };
    assert.equal(restored.title, "Ship capability bundle");

    bundle.setTaskContext({ taskId: "task-a", runId: "run-a" });
    await callTool(bundle.tools, "plan_set", {
      title: "Task A",
      steps: [{ text: "A1" }],
    });
    bundle.setTaskContext({ taskId: "task-b", runId: "run-b" });
    const isolated = (await callTool(bundle.tools, "plan_get", {})) as {
      title: string;
      steps: unknown[];
    };
    assert.equal(isolated.title, "");
    assert.deepEqual(isolated.steps, []);
    bundle.setTaskContext({ taskId: "task-a", runId: "run-a" });
    const taskAPlan = (await callTool(bundle.tools, "plan_get", {})) as {
      title: string;
    };
    assert.equal(taskAPlan.title, "Task A");
    bundle.setTaskContext();

    const memory = (await callTool(bundle.tools, "memory_remember", {
      text: "Channel MCP is the communication boundary",
      tags: ["architecture"],
    })) as { id: string };
    assert.ok(memory.id);
    const search = (await callTool(bundle.tools, "memory_search", {
      query: "communication boundary",
    })) as { matches: Array<{ id: string }> };
    assert.deepEqual(search.matches.map((entry) => entry.id), [memory.id]);

    const delegated = (await callTool(bundle.tools, "subagent", {
      tasks: [{ label: "audit", objective: "Audit the capability boundary" }],
    })) as { results: Array<{ ok: boolean; text: string }> };
    assert.equal(delegated.results[0]?.ok, true);
    assert.equal(delegated.results[0]?.text, "child result");
    assert.match(objectives[0] ?? "", /Audit the capability boundary/u);
  } finally {
    bundle.dispose();
  }
});

test("background tools own, report, and stop a session process", async () => {
  const root = temporaryDirectory();
  const workspace = join(root, "workspace");
  mkdirSync(workspace);
  const bundle = createBuiltinCapabilityBundle({
    cwd: workspace,
    mode: "local",
    sessionId: "session-background",
    sessionDir: join(root, "sessions"),
    principalId: "principal-1",
    subagentDepth: 0,
    subagentMaxDepth: 2,
    subagentConcurrency: 4,
    backgroundMaxProcesses: 2,
    createSubagent: async () => fakeConversation([]),
  });
  try {
    const started = (await callTool(bundle.tools, "background_start", {
      name: "fixture",
      command: `${JSON.stringify(process.execPath)} -e "setInterval(() => {}, 1000)"`,
    })) as { id: string; status: string; pid: number };
    assert.equal(started.status, "running");
    assert.ok(started.pid > 0);
    const listed = (await callTool(bundle.tools, "background_list", {})) as Array<{
      id: string;
    }>;
    assert.deepEqual(listed.map((item) => item.id), [started.id]);
    const stopped = (await callTool(bundle.tools, "background_stop", {
      processId: started.id,
    })) as { status: string };
    assert.equal(stopped.status, "stopped");
  } finally {
    bundle.dispose();
  }
});
