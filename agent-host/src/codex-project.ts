import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";

const DEFAULT_PROJECT_NAME = "AgentHub";

interface CodexGlobalState {
  "local-projects"?: Record<string, unknown>;
  "project-order"?: string[];
  "thread-project-assignments"?: Record<string, unknown>;
  [key: string]: unknown;
}

/**
 * Stable directory for Hub-executed Codex sessions. Registering this folder as
 * a project through `codex app` makes the Codex desktop app own the project, so
 * it persists across restarts (external edits to the app state are overwritten
 * when the app exits).
 */
export function agentHubProjectDir(): string {
  return resolve(
    process.env.AGENT_HUB_CODEX_PROJECT_DIR?.trim() ||
      join(homedir(), ".agenthub", "AgentHub"),
  );
}

/**
 * Deterministic fallback project id used only when the desktop app is
 * unavailable and we merge the project into the state file ourselves.
 */
export function agentHubProjectId(): string {
  const digest = createHash("sha256")
    .update("AgentSociety:AgentHub")
    .digest("hex");
  return [
    digest.slice(0, 8),
    digest.slice(8, 12),
    digest.slice(12, 16),
    digest.slice(16, 20),
    digest.slice(20, 32),
  ].join("-");
}

export function codexGlobalStatePath(): string {
  const home = process.env.CODEX_HOME?.trim() || join(homedir(), ".codex");
  return join(home, ".codex-global-state.json");
}

/**
 * Make sure the AgentHub project exists in the Codex desktop app and return its
 * project id. Preferred path: `codex app <dir>` lets the app itself register the
 * folder (persists across restarts). If that is unavailable (headless, no app
 * state, spawn disabled), falls back to a best-effort direct merge.
 */
export function ensureAgentHubProject(
  workspaceRoot?: string,
): string | undefined {
  if (process.env.AGENT_HUB_CODEX_PROJECT?.trim() === "0") return undefined;
  const dir = agentHubProjectDir();
  try {
    mkdirSync(dir, { recursive: true });
  } catch {
    // Continue; the project may already be registered.
  }
  let state = readGlobalState();
  const existing = state ? findProjectForRoot(state, dir) : undefined;
  let projectId = existing;
  if (!projectId && process.env.AGENT_HUB_CODEX_PROJECT_SPAWN !== "0") {
    try {
      spawnSync("codex", ["app", dir], {
        stdio: "ignore",
        timeout: 20_000,
      });
    } catch {
      // Fall back to the direct merge below.
    }
    for (let attempt = 0; attempt < 10 && !projectId; attempt += 1) {
      state = readGlobalState();
      projectId = state ? findProjectForRoot(state, dir) : undefined;
      if (!projectId) {
        // The app registers asynchronously; wait briefly between polls.
        Atomics.wait(
          new Int32Array(new SharedArrayBuffer(4)),
          0,
          0,
          300,
        );
      }
    }
  }
  if (!projectId) {
    projectId = mergeProjectIntoState(state ?? {});
  }
  if (projectId && workspaceRoot && workspaceRoot !== dir) {
    mergeRootIntoProject(projectId, workspaceRoot);
  }
  return projectId;
}

/**
 * Associate one Codex thread (for example a codex exec session created by the
 * bridge) with the AgentHub project. The desktop GUI currently hides exec-source
 * sessions, but the assignment keeps the data consistent and is picked up if a
 * future app version surfaces them.
 */
export function registerAgentHubThread(threadId: string, cwd: string): boolean {
  if (process.env.AGENT_HUB_CODEX_PROJECT?.trim() === "0") return false;
  if (!threadId.trim()) return false;
  const projectId = ensureAgentHubProject(cwd);
  if (!projectId) return false;
  const state = readGlobalState();
  if (!state) return false;
  const assignments = (state["thread-project-assignments"] ??= {});
  assignments[threadId.trim()] = {
    projectKind: "local",
    projectId,
    cwd: resolve(cwd),
    pendingCoreUpdate: false,
  };
  return writeGlobalState(state);
}

/**
 * Make a codex exec session visible in the Codex desktop GUI. The desktop app
 * hides threads whose source kind is `exec`; rewriting the session file and the
 * threads database entry to `cli` (a CLI session) makes the sidebar show it.
 * Best effort: each step is independent and failures are ignored.
 */
export function markCodexSessionVisible(threadId: string): boolean {
  if (process.env.AGENT_HUB_CODEX_PROJECT?.trim() === "0") return false;
  if (!threadId.trim()) return false;
  const sessionFile = findSessionFile(threadId);
  const fileOk = sessionFile
    ? rewriteSessionSource(sessionFile, threadId, "cli")
    : false;
  const dbOk = updateThreadSource(threadId, "cli");
  return fileOk || dbOk;
}

function findSessionFile(threadId: string): string | undefined {
  const home = process.env.CODEX_HOME?.trim() || join(homedir(), ".codex");
  const sessionsRoot = join(home, "sessions");
  if (!existsSync(sessionsRoot)) return undefined;
  const suffix = `${threadId}.jsonl`;
  let newest: { path: string; modified: number } | undefined;
  const visit = (directory: string): void => {
    for (const entry of readdirSync(directory)) {
      const path = join(directory, entry);
      let stats;
      try {
        stats = statSync(path);
      } catch {
        continue;
      }
      if (stats.isDirectory()) {
        visit(path);
        continue;
      }
      if (
        stats.isFile() &&
        entry.startsWith("rollout-") &&
        entry.endsWith(suffix) &&
        (!newest || stats.mtimeMs > newest.modified)
      ) {
        newest = { path, modified: stats.mtimeMs };
      }
    }
  };
  try {
    visit(sessionsRoot);
  } catch {
    return undefined;
  }
  return newest?.path;
}

function rewriteSessionSource(
  path: string,
  threadId: string,
  source: string,
): boolean {
  try {
    const lines = readFileSync(path, "utf8").split(/\r?\n/u);
    if (lines.length === 0) return false;
    const meta = JSON.parse(lines[0]!) as {
      type?: string;
      payload?: Record<string, unknown>;
    };
    if (
      meta.type !== "session_meta" ||
      typeof meta.payload !== "object" ||
      meta.payload === null ||
      meta.payload.id !== threadId
    ) {
      return false;
    }
    if (meta.payload.source === source) return true;
    meta.payload.source = source;
    lines[0] = JSON.stringify(meta);
    const mode = statSync(path).mode & 0o777;
    const temporary = `${path}.agenthub.tmp`;
    writeFileSync(temporary, lines.join("\n"), {
      encoding: "utf8",
      mode,
    });
    chmodSync(temporary, mode);
    renameSync(temporary, path);
    return true;
  } catch {
    return false;
  }
}

function updateThreadSource(threadId: string, source: string): boolean {
  const path = threadDbPath();
  if (!path || !existsSync(path)) return false;
  backupThreadDb(path);
  try {
    const db = new DatabaseSync(path, { timeout: 2_000 });
    try {
      const result = db
        .prepare(
          "UPDATE threads SET source = ? WHERE id = ? AND source = 'exec'",
        )
        .run(source, threadId);
      return Number(result.changes) > 0;
    } finally {
      db.close();
    }
  } catch {
    return false;
  }
}

/**
 * Locate the desktop app's threads database. The app names it with a schema
 * version (`state_5.sqlite` today); prefer the newest `state_*.sqlite` that
 * actually has a `threads` table so app upgrades keep working.
 */
function threadDbPath(): string | undefined {
  const home = process.env.CODEX_HOME?.trim() || join(homedir(), ".codex");
  let entries: string[] = [];
  try {
    entries = readdirSync(home);
  } catch {
    return undefined;
  }
  const candidates = entries
    .filter((entry) => /^state_(\d+)\.sqlite$/u.test(entry))
    .map((entry) => {
      const version = Number(entry.match(/^state_(\d+)\.sqlite$/u)?.[1]);
      return { path: join(home, entry), version };
    })
    .sort((left, right) => right.version - left.version)
    .map((entry) => entry.path);
  for (const path of candidates) {
    try {
      const db = new DatabaseSync(path, { readOnly: true });
      try {
        const row = db
          .prepare(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'",
          )
          .get();
        if (row) return path;
      } finally {
        db.close();
      }
    } catch {
      // Try the next candidate.
    }
  }
  return candidates[0];
}

function backupThreadDb(path: string): void {
  const backup = `${path}.agenthub-bak`;
  if (existsSync(backup)) return;
  try {
    const db = new DatabaseSync(path, { timeout: 2_000 });
    try {
      db.exec(`VACUUM INTO '${backup.replace(/'/gu, "''")}'`);
    } finally {
      db.close();
    }
  } catch {
    // The app may hold the DB; skip the one-time backup in that case.
  }
}

function findProjectForRoot(
  state: CodexGlobalState,
  root: string,
): string | undefined {
  const projects = state["local-projects"];
  if (!projects) return undefined;
  for (const [id, raw] of Object.entries(projects)) {
    if (typeof raw !== "object" || raw === null) continue;
    const rootPaths = (raw as Record<string, unknown>).rootPaths;
    if (
      Array.isArray(rootPaths) &&
      rootPaths.some(
        (entry) => typeof entry === "string" && resolve(entry) === root,
      )
    ) {
      return id;
    }
  }
  return undefined;
}

function mergeProjectIntoState(state: CodexGlobalState): string | undefined {
  const projectId = agentHubProjectId();
  const now = Date.now();
  const projects = (state["local-projects"] ??= {});
  projects[projectId] = {
    id: projectId,
    name:
      process.env.AGENT_HUB_CODEX_PROJECT_NAME?.trim() || DEFAULT_PROJECT_NAME,
    rootPaths: [agentHubProjectDir()],
    createdAt: now,
    updatedAt: now,
  };
  const order = state["project-order"];
  if (!Array.isArray(order)) {
    state["project-order"] = [projectId];
  } else if (!order.includes(projectId)) {
    order.push(projectId);
  }
  return writeGlobalState(state) ? projectId : undefined;
}

function mergeRootIntoProject(projectId: string, root: string): boolean {
  const state = readGlobalState();
  if (!state) return false;
  const raw = state["local-projects"]?.[projectId];
  if (typeof raw !== "object" || raw === null) return false;
  const project = raw as Record<string, unknown>;
  const rootPaths = Array.isArray(project.rootPaths)
    ? [...(project.rootPaths as unknown[])]
    : [];
  const resolved = resolve(root);
  if (
    !rootPaths.some(
      (entry) => typeof entry === "string" && resolve(entry) === resolved,
    )
  ) {
    rootPaths.push(resolved);
    project.rootPaths = rootPaths;
    project.updatedAt = Date.now();
    return writeGlobalState(state);
  }
  return false;
}

function readGlobalState(): CodexGlobalState | undefined {
  const path = codexGlobalStatePath();
  if (!existsSync(path)) return undefined;
  try {
    const raw = JSON.parse(readFileSync(path, "utf8")) as unknown;
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
      return undefined;
    }
    return raw as CodexGlobalState;
  } catch {
    return undefined;
  }
}

function writeGlobalState(state: CodexGlobalState): boolean {
  try {
    const path = codexGlobalStatePath();
    if (!existsSync(path)) return false;
    const mode = statSync(path).mode & 0o777;
    const temporary = join(
      dirname(path),
      `.codex-global-state.json.agenthub-${process.pid}.tmp`,
    );
    writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, {
      encoding: "utf8",
      mode,
    });
    chmodSync(temporary, mode);
    renameSync(temporary, path);
    return true;
  } catch {
    return false;
  }
}
