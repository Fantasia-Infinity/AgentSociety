import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";

import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";
import type { PiAgentEngine } from "./pi-engine.js";
import { RunSessionRegistry } from "./run-registry.js";

export async function runInteractive(
  config: AgentHostConfig,
  hub?: HubClient,
): Promise<void> {
  const run = hub
    ? await hub.startRun({
        principal_id: config.principalId,
        actor_id: config.actorId,
        node_id: config.nodeId,
        origin: "local_ui",
        objective: "Interactive Pi TUI controlled by the signed-in local user",
        metadata: { client: "pi-tui" },
      })
    : undefined;
  const runId = run?.run_id ?? `local_${randomUUID().replaceAll("-", "")}`;
  const registry = new RunSessionRegistry(config.sessionDir);
  try {
    const exitCode = await runTuiChild(runId, !hub);
    if (exitCode !== 0) throw new Error(`Pi TUI exited with code ${exitCode}`);
    const session = registry.get(runId);
    registry.updateStatus(runId, "completed");
    if (hub && run) {
      await hub.updateRun(run.run_id, {
        status: "completed",
        result: session ? { pi_session_id: session.sessionId } : {},
      });
    }
  } catch (error) {
    registry.updateStatus(runId, "failed");
    if (hub && run) {
      await hub.updateRun(run.run_id, {
        status: "failed",
        result: {},
        error: error instanceof Error ? error.message : String(error),
      });
    }
    throw error;
  }
}

export async function runInteractiveChild(
  config: AgentHostConfig,
  engine: PiAgentEngine,
  runId: string,
): Promise<void> {
  const registry = new RunSessionRegistry(config.sessionDir);
  await engine.runTui({
    cwd: config.workspaceRoot,
    onSessionReady: (session) => {
      if (!session.sessionFile) return;
      registry.upsert({
        runId,
        sessionId: session.sessionId,
        sessionFile: session.sessionFile,
        cwd: config.workspaceRoot,
        origin: "local_ui",
        status: "active",
      });
    },
  });
}

async function runTuiChild(runId: string, hubDisabled: boolean): Promise<number> {
  const entrypoint = process.argv[1];
  if (!entrypoint) throw new Error("Could not determine Agent Host entrypoint");
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [entrypoint, "__tui-child", runId], {
      stdio: "inherit",
      env: {
        ...process.env,
        ...(hubDisabled ? { AGENT_HUB_RUNTIME_DISABLED: "1" } : {}),
      },
    });
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      if (signal) {
        reject(new Error(`Pi TUI stopped by ${signal}`));
        return;
      }
      resolve(code ?? 1);
    });
  });
}
