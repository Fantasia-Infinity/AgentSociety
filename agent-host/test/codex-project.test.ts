import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import { DatabaseSync } from "node:sqlite";

import {
  agentHubProjectDir,
  agentHubProjectId,
  ensureAgentHubProject,
  markCodexSessionVisible,
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
  delete process.env.AGENT_HUB_CODEX_PROJECT_DIR;
  delete process.env.AGENT_HUB_CODEX_PROJECT_SPAWN;
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

test("ensureAgentHubProject adds a fallback project idempotently without losing state", () => {
  const home = temporaryHome();
  process.env.CODEX_HOME = home;
  process.env.AGENT_HUB_CODEX_PROJECT_SPAWN = "0";
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

  const workspace = join(home, "extra-workspace");
  assert.equal(ensureAgentHubProject(workspace), agentHubProjectId());
  assert.equal(ensureAgentHubProject(workspace), agentHubProjectId());

  const state = readState(home);
  const projects = state["local-projects"] as Record<string, unknown>;
  const project = projects[agentHubProjectId()] as Record<string, unknown>;
  assert.equal(project.name, "AgentHub");
  assert.deepEqual(
    project.rootPaths,
    [agentHubProjectDir(), workspace].sort(),
  );
  assert.ok(
    (state["project-order"] as string[]).includes(agentHubProjectId()),
  );
  assert.equal(
    (projects[otherId] as Record<string, unknown>).name,
    "agentsociety",
  );
  assert.equal(
    (state["electron-persisted-atom-state"] as Record<string, unknown>).kept,
    true,
  );
  assert.ok(existsSync(agentHubProjectDir()));
});

test("ensureAgentHubProject reuses an app-created project for the same root", () => {
  const home = temporaryHome();
  process.env.CODEX_HOME = home;
  process.env.AGENT_HUB_CODEX_PROJECT_SPAWN = "0";
  const appProjectId = "f99d3694-1f1c-45a6-80f8-2a270d5fa3fd";
  writeState(home, {
    "local-projects": {
      [appProjectId]: {
        id: appProjectId,
        name: "AgentHub",
        rootPaths: [agentHubProjectDir()],
        createdAt: 1,
        updatedAt: 1,
      },
    },
    "project-order": [appProjectId],
    "thread-project-assignments": {},
  });

  assert.equal(ensureAgentHubProject(), appProjectId);
  assert.equal(
    registerAgentHubThread(
      "019fcc2c-7a47-7612-9c3a-a2404d5957ab",
      agentHubProjectDir(),
    ),
    true,
  );
  const state = readState(home);
  const assignments = state[
    "thread-project-assignments"
  ] as Record<string, unknown>;
  assert.deepEqual(assignments["019fcc2c-7a47-7612-9c3a-a2404d5957ab"], {
    projectKind: "local",
    projectId: appProjectId,
    cwd: agentHubProjectDir(),
    pendingCoreUpdate: false,
  });
  assert.equal(
    agentHubProjectId() in (state["local-projects"] as Record<string, unknown>),
    false,
  );
});

test("registration is skipped when disabled or state file is missing", () => {
  const home = temporaryHome();
  process.env.CODEX_HOME = home;
  process.env.AGENT_HUB_CODEX_PROJECT_SPAWN = "0";
  process.env.AGENT_HUB_CODEX_PROJECT = "0";
  assert.equal(ensureAgentHubProject(home), undefined);
  assert.equal(registerAgentHubThread("thread-1", home), false);
  delete process.env.AGENT_HUB_CODEX_PROJECT;
  assert.equal(ensureAgentHubProject(home), undefined);
  assert.equal(
    existsSync(join(home, ".codex-global-state.json")),
    false,
  );
});

test("markCodexSessionVisible rewrites the session file and threads database", () => {
  const home = temporaryHome();
  process.env.CODEX_HOME = home;
  process.env.AGENT_HUB_CODEX_PROJECT_SPAWN = "0";
  const sid = "019fcc2c-7a47-7612-9c3a-a2404d5957ab";
  const sessionDir = join(home, "sessions", "2026", "08", "04");
  mkdirSync(sessionDir, { recursive: true });
  const sessionPath = join(
    sessionDir,
    `rollout-2026-08-04T11-48-14-${sid}.jsonl`,
  );
  writeFileSync(
    sessionPath,
    `${JSON.stringify({
      timestamp: "2026-08-04T09:48:14.792Z",
      type: "session_meta",
      payload: { id: sid, source: "exec", cwd: agentHubProjectDir() },
    })}\n{"timestamp":"x","type":"event_msg"}\n`,
  );
  const dbPath = join(home, "state_5.sqlite");
  const db = new DatabaseSync(dbPath);
  db.exec(
    "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT NOT NULL, cwd TEXT)",
  );
  db.prepare("INSERT INTO threads (id, source, cwd) VALUES (?, ?, ?)").run(
    sid,
    "exec",
    agentHubProjectDir(),
  );
  db.close();

  assert.equal(markCodexSessionVisible(sid), true);
  const firstLine = JSON.parse(readFileSync(sessionPath, "utf8").split("\n")[0]!)
    .payload as Record<string, unknown>;
  assert.equal(firstLine.source, "cli");
  const reopened = new DatabaseSync(dbPath, { readOnly: true });
  const row = reopened
    .prepare("SELECT source FROM threads WHERE id = ?")
    .get(sid) as Record<string, unknown> | undefined;
  assert.equal(
    row?.source,
    "cli",
  );
  reopened.close();
  assert.ok(existsSync(`${dbPath}.agenthub-bak`));
});

test("markCodexSessionVisible is skipped when disabled", () => {
  const home = temporaryHome();
  process.env.CODEX_HOME = home;
  process.env.AGENT_HUB_CODEX_PROJECT = "0";
  const sid = "019fcc2c-7a47-7612-9c3a-a2404d5957ab";
  const sessionDir = join(home, "sessions", "2026", "08", "04");
  mkdirSync(sessionDir, { recursive: true });
  const sessionPath = join(
    sessionDir,
    `rollout-2026-08-04T11-48-14-${sid}.jsonl`,
  );
  writeFileSync(
    sessionPath,
    JSON.stringify({
      type: "session_meta",
      payload: { id: sid, source: "exec" },
    }),
  );
  assert.equal(markCodexSessionVisible(sid), false);
  assert.match(readFileSync(sessionPath, "utf8"), /"source":"exec"/);
});
