import { spawn, spawnSync } from "node:child_process";
import { closeSync, existsSync, openSync } from "node:fs";
import { resolve } from "node:path";

import type { AgentHostConfig } from "./config.js";
import type { HubTask } from "./types.js";

/**
 * Self-update task mode.
 *
 * A delegated task whose input contains `action: "self_update"` is executed by
 * the worker process itself (not by an LLM session): it pulls the latest code,
 * reinstalls pinned dependencies, reapplies the security patch, rebuilds, and
 * then restarts the worker so the new code takes effect. This works even when
 * AGENT_REMOTE_TOOL_POLICY=read_only because the shell work runs in the worker,
 * not through model tools. Restart only happens after the task result has been
 * durably reported to the Hub.
 */

export const SELF_UPDATE_ACTION = "self_update";

export interface SelfUpdateReport {
  ok: boolean;
  updated: boolean;
  steps: string[];
  error?: string;
  before?: string;
  after?: string;
}

export function isSelfUpdateTask(task: HubTask): boolean {
  return task.input?.action === SELF_UPDATE_ACTION;
}

export function selfUpdateBranch(task: HubTask): string {
  const requested = task.input?.branch;
  return typeof requested === "string" && requested.trim()
    ? requested.trim()
    : "main";
}

export function runSelfUpdate(
  config: AgentHostConfig,
  task: HubTask,
  cwd: string,
): SelfUpdateReport {
  if (!config.selfUpdateEnabled) {
    throw new Error("Self-update is disabled on this host (AGENT_SELF_UPDATE=0)");
  }
  const steps: string[] = [];
  const record = (label: string, output: string) => {
    const trimmed = output.trim();
    steps.push(trimmed ? `${label}: ${trimmed.slice(0, 800)}` : label);
  };
  const agentHostDir = resolve(cwd, "agent-host");
  const branch = selfUpdateBranch(task);
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";

  const before = run(
    cwd,
    "git",
    ["rev-parse", "--short", "HEAD"],
    record,
    "Current commit",
  );
  run(cwd, "git", ["fetch", "origin"], record, "Fetch origin");
  run(
    cwd,
    "git",
    ["pull", "--ff-only", "origin", branch],
    record,
    `Pull origin/${branch}`,
  );
  const after = run(
    cwd,
    "git",
    ["rev-parse", "--short", "HEAD"],
    record,
    "Updated commit",
  );
  const updated = after.trim() !== before.trim();

  if (updated) {
    run(agentHostDir, npm, ["ci", "--ignore-scripts"], record, "npm ci");
    run(
      agentHostDir,
      process.execPath,
      ["scripts/patch-pi-brace-expansion.mjs"],
      record,
      "Apply security patch",
    );
    run(
      agentHostDir,
      process.execPath,
      ["scripts/patch-pi-brace-expansion.mjs", "--check"],
      record,
      "Verify security patch",
    );
    run(agentHostDir, npm, ["run", "build"], record, "Build");
  }

  return {
    ok: true,
    updated,
    steps,
    before: before.trim(),
    after: after.trim(),
  };
}

export function restartWorker(config: AgentHostConfig): void {
  const cli = resolve(
    config.workspaceRoot,
    "agent-host",
    "dist",
    "src",
    "cli.js",
  );
  if (!existsSync(cli)) {
    throw new Error(`Worker entrypoint missing: ${cli}`);
  }
  const logPath = resolve(config.workspaceRoot, "agent-host", "worker-restart.log");
  const logFd = openSync(logPath, "a");
  const child = spawn(process.execPath, [cli, "worker"], {
    detached: true,
    stdio: ["ignore", logFd, logFd],
    windowsHide: true,
    env: process.env,
  });
  child.unref();
  closeSync(logFd);
}

function run(
  cwd: string,
  command: string,
  args: string[],
  record: (label: string, output: string) => void,
  label: string,
): string {
  const result = spawnSync(command, args, {
    cwd,
    encoding: "utf8",
    maxBuffer: 32 * 1024 * 1024,
  });
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`.trim();
  if (result.status !== 0) {
    throw new Error(
      `${label} failed (exit ${result.status ?? "?"}): ${output.slice(-2000)}`,
    );
  }
  record(label, output);
  return output;
}
