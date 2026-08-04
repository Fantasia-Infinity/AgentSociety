import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import {
  agentHubProjectId,
  ensureAgentHubProject,
  registerAgentHubThread,
} from "../src/codex-project.js";

const savedCodexHome = process.env.CODEX_HOME;
const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const path of temporaryDirectories.splice(0)) {
    rmSync(path, { recursive: true, force: true });
  }
  if (savedCodexHome === undefined) {
    delete process.env.CODEX_HOME;
  } else {
    process.env.CODEX_HOME = savedCodexHome;
  }
  delete process.env.AGENT_HUB_CODEX_PROJECT;
  delete process.env.AGENT_HUB_CODEX_PROJECT_NAME;
});

function temporaryHome(): string {
  const path = mkdtempSync(join(tmpdir(), "codex-project-test-"));
  temporaryDirectories.push(path);
  return path;
}

function writeState(home: string, state: unknown): void {
  mkdirSync(home, { recursive: true });
  writeFileSync(
    join(home, ".codex-global-state.json"),
    JSON.stringify(state, null, 2),
  );
}

function readState(home: string): Record<string, unknown> {
  return JSON.parse(
    readFileSync(join(home, ".codex-global-state.json"), "utf8"),
  ) as Record<string, unknown>;
}

test("ensureAgentHubProject adds the project idempotently without losing state", () => {
  const home = temporaryHome();
  process.env.CODEX_HOME = home;
  const otherId = "c5beb5fa-dcb8-483b-9522-0da7bb5ae116";
  writeState(home, {
    "local-projects": {
      [otherId]: {
        id: otherId,
        name: "agentsociety",
        rootPaths: ["/x"],
        createdAt: 1,
        updatedAt: 1,
      },
    },
    "project-order": [otherId],
    "thread-project-assignments": {
      "old-thread": {
        projectKind: "local",
        projectId: otherId,
        cwd: "/x",
        pendingCoreUpdate: false,
      },
    },
    "electron-persisted-atom-state": { kept: true },
  });

  const workspace = join(home, "workspace");
  assert.equal(ensureAgentHubProject(workspace), true);
  assert.equal(ensureAgentHubProject(workspace), true);

  const state = readState(home);
  const projects = state["local-projects"] as Record<string, unknown>;
  const project = projects[agentHubProjectId()] as Record<string, unknown>;
  assert.equal(project.name, "AgentHub");
  assert.deepEqual(project.rootPaths, [workspace]);
  assert.ok(
    (state["project-order"] as string[]).includes(agentHubProjectId()),
  );
  assert.equal(
    (projects[otherId] as Record<string, unknown>).name,
    "agentsociety",
  );
  assert.equal(
    (
      (state["electron-persisted-atom-state"] as Record<string, unknown>)
        .kept
    ),
    true,
  );
});

test("registerAgentHubThread associates a thread with the AgentHub project", () => {
  const home = temporaryHome();
  process.env.CODEX_HOME = home;
  writeState(home, {
    "local-projects": {},
    "project-order": [],
    "thread-project-assignments": {},
  });

  const workspace = join(home, "workspace");
  assert.equal(
    registerAgentHubThread(
      "019fcc11-c175-7540-ad53-41b9cae47e62",
      workspace,
    ),
    true,
  );
  const state = readState(home);
  const assignments = state[
    "thread-project-assignments"
  ] as Record<string, unknown>;
  assert.deepEqual(
    assignments["019fcc11-c175-7540-ad53-41b9cae47e62"],
    {
      projectKind: "local",
      projectId: agentHubProjectId(),
      cwd: workspace,
      pendingCoreUpdate: false,
    },
  );
});

test("registration is skipped when disabled or state file is missing", () => {
  const home = temporaryHome();
  process.env.CODEX_HOME = home;
  process.env.AGENT_HUB_CODEX_PROJECT = "0";
  assert.equal(ensureAgentHubProject(home), false);
  assert.equal(registerAgentHubThread("thread-1", home), false);
  delete process.env.AGENT_HUB_CODEX_PROJECT;
  assert.equal(ensureAgentHubProject(home), false);
});
