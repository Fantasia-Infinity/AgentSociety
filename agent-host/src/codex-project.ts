import { createHash } from "node:crypto";
import {
  chmodSync,
  existsSync,
  readFileSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";

const DEFAULT_PROJECT_NAME = "AgentHub";

interface CodexGlobalState {
  "local-projects"?: Record<string, unknown>;
  "project-order"?: string[];
  "thread-project-assignments"?: Record<string, unknown>;
  [key: string]: unknown;
}

/**
 * Stable project id for the unified "AgentHub" Codex project. The desktop app
 * stores projects in `.codex-global-state.json` under `local-projects` keyed by
 * a UUID; deriving it deterministically keeps the id stable across machines
 * and restarts without extra state files.
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
 * Register (or refresh) the AgentHub project in the Codex desktop app state so
 * sessions executed through the Hub are grouped under one project. Best effort:
 * returns false and never throws when the app state is missing or malformed.
 */
export function ensureAgentHubProject(workspaceRoot: string): boolean {
  if (process.env.AGENT_HUB_CODEX_PROJECT?.trim() === "0") return false;
  const state = readGlobalState();
  if (!state) return false;
  const projectId = agentHubProjectId();
  const now = Date.now();
  const projects = (state["local-projects"] ??= {});
  const existing = projects[projectId] as Record<string, unknown> | undefined;
  const rootPaths = new Set<string>(
    Array.isArray(existing?.rootPaths)
      ? (existing.rootPaths as unknown[]).filter(
          (entry): entry is string => typeof entry === "string",
        )
      : [],
  );
  rootPaths.add(resolve(workspaceRoot));
  projects[projectId] = {
    id: projectId,
    name: process.env.AGENT_HUB_CODEX_PROJECT_NAME?.trim() || DEFAULT_PROJECT_NAME,
    rootPaths: [...rootPaths],
    createdAt: existing?.createdAt ?? now,
    updatedAt: now,
  };
  const order = state["project-order"];
  if (!Array.isArray(order)) {
    state["project-order"] = [projectId];
  } else if (!order.includes(projectId)) {
    order.push(projectId);
  }
  return writeGlobalState(state);
}

/**
 * Associate one Codex thread (for example a codex exec session created by the
 * bridge) with the AgentHub project so the desktop sidebar shows it there.
 */
export function registerAgentHubThread(threadId: string, cwd: string): boolean {
  if (process.env.AGENT_HUB_CODEX_PROJECT?.trim() === "0") return false;
  if (!threadId.trim()) return false;
  const resolved = resolve(cwd);
  if (!ensureAgentHubProject(resolved)) return false;
  const state = readGlobalState();
  if (!state) return false;
  const assignments = (state["thread-project-assignments"] ??= {});
  assignments[threadId.trim()] = {
    projectKind: "local",
    projectId: agentHubProjectId(),
    cwd: resolved,
    pendingCoreUpdate: false,
  };
  return writeGlobalState(state);
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
