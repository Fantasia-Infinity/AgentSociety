import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  WorkerSessionRegistry,
  type WorkerSessionScope,
} from "../src/worker-session-registry.js";

test("worker session registry isolates principals, workspaces, and slots", () => {
  const root = mkdtempSync(join(tmpdir(), "worker-session-registry-"));
  try {
    const sessionDir = join(root, "sessions");
    const workspace = join(root, "workspace");
    mkdirSync(sessionDir);
    mkdirSync(workspace);
    const sessionFile = join(sessionDir, "session.jsonl");
    writeFileSync(sessionFile, "{}\n");
    const registry = new WorkerSessionRegistry(sessionDir);
    const scope: WorkerSessionScope = {
      actorId: "actor-a",
      nodeId: "node-a",
      principalId: "principal-a",
      workerSlot: 0,
      cwd: workspace,
    };

    const first = registry.upsert(
      scope,
      { sessionId: "session-a", sessionFile },
      "task-1",
    );
    const second = registry.upsert(
      scope,
      { sessionId: "session-a", sessionFile },
      "task-2",
    );
    assert.equal(first.taskCount, 1);
    assert.equal(second.taskCount, 2);
    assert.equal(registry.get(scope)?.lastTaskId, "task-2");
    assert.notEqual(
      registry.key(scope),
      registry.key({ ...scope, principalId: "principal-b" }),
    );
    assert.notEqual(
      registry.key(scope),
      registry.key({ ...scope, workerSlot: 1 }),
    );
    const otherWorkspace = join(root, "other-workspace");
    mkdirSync(otherWorkspace);
    assert.notEqual(
      registry.key(scope),
      registry.key({ ...scope, cwd: otherWorkspace }),
    );
    assert.throws(
      () =>
        registry.upsert(
          scope,
          { sessionId: "escaped", sessionFile: join(root, "outside.jsonl") },
          "task-3",
        ),
      /inside AGENT_SESSION_DIR/u,
    );
    if (process.platform !== "win32") {
      assert.equal(
        statSync(join(sessionDir, "agent-host-worker-sessions.d")).mode & 0o777,
        0o700,
      );
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
