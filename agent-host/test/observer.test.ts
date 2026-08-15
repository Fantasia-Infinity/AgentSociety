import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import { readTranscript } from "../src/observer.js";
import type { RunSessionRecord } from "../src/run-registry.js";

const temporaryDirectories: string[] = [];
afterEach(() => {
  for (const path of temporaryDirectories.splice(0)) {
    rmSync(path, { recursive: true, force: true });
  }
});

function record(
  path: string,
  overrides: Partial<RunSessionRecord> = {},
): RunSessionRecord {
  return {
    runId: "run-1",
    taskId: "task-1",
    sessionId: "dsh-session",
    sessionFile: path,
    cwd: "/tmp/workspace",
    origin: "remote_task",
    engine: "dsh",
    status: "completed",
    startedAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    ...overrides,
  };
}

test("dsh marker and plain JSONL transcript are rendered by the observer", () => {
  const directory = mkdtempSync(join(tmpdir(), "agent-host-observe-"));
  temporaryDirectories.push(directory);
  const marker = join(directory, "session.agent-society.json");
  const transcript = join(directory, "session.jsonl");
  writeFileSync(
    marker,
    `${JSON.stringify({ version: 1, engine: "dsh", transcriptFile: transcript })}\n`,
  );
  writeFileSync(
    transcript,
    [
      JSON.stringify({ type: "session", version: 0, id: "dsh-session" }),
      JSON.stringify({
        type: "user/message",
        data: { content: [{ type: "text", text: "hello" }] },
      }),
      JSON.stringify({
        type: "assistant/message",
        data: {
          message: {
            role: "assistant",
            content: [{ type: "text", text: "done" }],
          },
        },
      }),
    ].join("\n"),
  );
  const messages = readTranscript(record(marker), 20);
  assert.deepEqual(messages, ["user> hello", "assistant> done"]);
});

test("dsh marker without a transcript reports a clear message", () => {
  const directory = mkdtempSync(join(tmpdir(), "agent-host-observe-"));
  temporaryDirectories.push(directory);
  const marker = join(directory, "session.agent-society.json");
  writeFileSync(
    marker,
    `${JSON.stringify({
      version: 1,
      engine: "dsh",
      transcriptFile: join(directory, "missing.jsonl"),
    })}\n`,
  );
  assert.deepEqual(readTranscript(record(marker), 20), [
    "DeepSeek Harness session transcript has not been created yet.",
  ]);
});
