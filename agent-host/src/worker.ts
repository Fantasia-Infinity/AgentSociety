import { existsSync, statSync } from "node:fs";
import { relative, resolve } from "node:path";

import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";
import type { AgentEngine, HubClaim, HubTask } from "./types.js";

type WorkerHub = Pick<HubClient, "claimTask" | "updateTask" | "heartbeat">;

export class TaskWorker {
  constructor(
    private readonly config: AgentHostConfig,
    private readonly hub: WorkerHub,
    private readonly engine: AgentEngine,
    private readonly output: (message: string) => void = console.log,
  ) {}

  async runOnce(waitSeconds = 0): Promise<boolean> {
    const claim = await this.hub.claimTask({
      actor_id: this.config.actorId,
      node_id: this.config.nodeId,
      wait_seconds: waitSeconds,
      lease_seconds: this.config.leaseSeconds,
    });
    if (!claim) return false;
    await this.execute(claim);
    return true;
  }

  async runForever(signal: AbortSignal): Promise<void> {
    this.output(
      `Pi worker ready as ${this.config.actorId} on ${this.config.nodeId}`,
    );
    while (!signal.aborted) {
      try {
        await this.hub.heartbeat(this.config.nodeId);
        await this.runOnce(this.config.pollSeconds);
      } catch (error) {
        if (signal.aborted) return;
        this.output(`Worker error: ${errorMessage(error)}`);
        await abortableDelay(2_000, signal);
      }
    }
  }

  private async execute(claim: HubClaim): Promise<void> {
    const { task, run, lease_token: leaseToken } = claim;
    const cwd = resolveTaskWorkspace(this.config.workspaceRoot, task);
    this.output(`Claimed ${task.task_id}: ${task.objective}`);
    await this.hub.updateTask(task.task_id, {
      run_id: run.run_id,
      lease_token: leaseToken,
      status: "working",
      message: "Pi session starting",
    });

    let renewalRunning = false;
    const renewal = setInterval(() => {
      if (renewalRunning) return;
      renewalRunning = true;
      void this.hub
        .updateTask(task.task_id, {
          run_id: run.run_id,
          lease_token: leaseToken,
          status: "working",
          message: "Pi session active",
        })
        .catch((error: unknown) => {
          this.output(`Lease renewal failed: ${errorMessage(error)}`);
        })
        .finally(() => {
          renewalRunning = false;
        });
    }, 60_000);

    let conversation: Awaited<ReturnType<AgentEngine["createConversation"]>> | undefined;
    try {
      conversation = await this.engine.createConversation({
        cwd,
        mode: "remote",
        persisted: false,
      });
      const result = await conversation.prompt(taskPrompt(task, cwd));
      await this.hub.updateTask(task.task_id, {
        run_id: run.run_id,
        lease_token: leaseToken,
        status: "completed",
        message: "Pi session completed",
        result: {
          text: result.text,
          provider: result.provider,
          model: result.model,
          session_id: result.sessionId,
        },
      });
      this.output(`Completed ${task.task_id}`);
    } catch (error) {
      const message = errorMessage(error);
      try {
        await this.hub.updateTask(task.task_id, {
          run_id: run.run_id,
          lease_token: leaseToken,
          status: "failed",
          message,
          result: {},
        });
      } catch (updateError) {
        this.output(
          `Could not report failure for ${task.task_id}: ${errorMessage(updateError)}`,
        );
      }
      throw error;
    } finally {
      clearInterval(renewal);
      conversation?.dispose();
    }
  }
}

export function resolveTaskWorkspace(root: string, task: HubTask): string {
  const requested =
    typeof task.input.workspace === "string" ? task.input.workspace : ".";
  const candidate = resolve(root, requested);
  const pathFromRoot = relative(root, candidate);
  if (
    pathFromRoot === ".." ||
    pathFromRoot.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`)
  ) {
    throw new Error("Task workspace escapes AGENT_WORKSPACE_ROOT");
  }
  if (!existsSync(candidate) || !statSync(candidate).isDirectory()) {
    throw new Error("Task workspace does not exist or is not a directory");
  }
  return candidate;
}

function taskPrompt(task: HubTask, cwd: string): string {
  return [
    "You are executing a durable task delegated through the collaboration Hub.",
    `Task ID: ${task.task_id}`,
    `Objective: ${task.objective}`,
    `Configured workspace: ${cwd}`,
    `Structured input: ${JSON.stringify(task.input)}`,
    "Complete the objective with the currently available tools. Return a concise result suitable for the delegating agent. Do not claim actions you did not perform.",
  ].join("\n");
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return;
  await new Promise<void>((done) => {
    const timer = setTimeout(done, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        done();
      },
      { once: true },
    );
  });
}
