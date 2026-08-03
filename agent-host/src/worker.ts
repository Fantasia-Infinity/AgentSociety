import { existsSync, statSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";

import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";
import { RunSessionRegistry } from "./run-registry.js";
import {
  isSelfUpdateTask,
  restartWorker,
  runSelfUpdate,
  type SelfUpdateReport,
} from "./self-update.js";
import type { AgentEngine, HubClaim, HubTask } from "./types.js";

type WorkerHub = Pick<
  HubClient,
  "claimTask" | "updateTask" | "updateRun" | "heartbeat"
> &
  Partial<
    Pick<
      HubClient,
      "getTask" | "claimTaskControls" | "acknowledgeTaskControl"
    >
  >;

export class TaskWorker {
  private readonly registry: RunSessionRegistry;

  constructor(
    private readonly config: AgentHostConfig,
    private readonly hub: WorkerHub,
    private readonly engine: AgentEngine,
    private readonly output: (message: string) => void = console.log,
    private readonly restart: () => void = () => {
      restartWorker(config);
      process.exit(0);
    },
  ) {
    this.registry = new RunSessionRegistry(config.sessionDir);
  }

  async runOnce(waitSeconds = 0, signal?: AbortSignal): Promise<boolean> {
    const claim = await this.hub.claimTask({
      actor_id: this.config.actorId,
      node_id: this.config.nodeId,
      wait_seconds: waitSeconds,
      lease_seconds: this.config.leaseSeconds,
    }, signal);
    if (!claim) return false;
    const shouldRestart = await this.execute(claim, signal);
    if (shouldRestart) {
      this.output("Self-update applied; restarting worker");
      this.restart();
    }
    return true;
  }

  async runForever(signal: AbortSignal): Promise<void> {
    this.output(
      `Pi worker ready as ${this.config.actorId} on ${this.config.nodeId}`,
    );
    while (!signal.aborted) {
      try {
        await this.hub.heartbeat(this.config.nodeId);
        await this.runOnce(this.config.pollSeconds, signal);
      } catch (error) {
        if (signal.aborted) return;
        this.output(`Worker error: ${errorMessage(error)}`);
        await abortableDelay(2_000, signal);
      }
    }
  }

  private async applySessionTitle(
    task: HubTask,
    cwd: string,
    conversation: Awaited<ReturnType<AgentEngine["createConversation"]>>,
  ): Promise<void> {
    // Ask a throwaway in-memory Pi session to summarize the objective into a
    // short session title. This keeps the summarizing exchange out of the
    // durable task session, and a failure never blocks the task itself.
    let titleSession: Awaited<ReturnType<AgentEngine["createConversation"]>> | undefined;
    try {
      titleSession = await this.engine.createConversation({
        cwd,
        mode: "remote",
        persisted: false,
      });
      const result = await titleSession.prompt(
        [
          "Summarize the following task objective into a short session title.",
          "Rules:",
          "- Use the same language as the objective",
          "- At most 40 characters",
          "- Output only the title, no quotes, no explanation",
          `Objective: ${task.objective}`,
        ].join("\n"),
      );
      const title = cleanSessionTitle(result.text);
      if (title) {
        conversation.setSessionName(title);
        this.output(`Session title: ${title}`);
      }
    } catch (error) {
      this.output(`Session title generation skipped: ${errorMessage(error)}`);
    } finally {
      await titleSession?.dispose();
    }
  }

  private async execute(
    claim: HubClaim,
    signal?: AbortSignal,
  ): Promise<boolean> {
    const { task, run, lease_token: leaseToken } = claim;
    let cwd: string;
    try {
      cwd = isSelfUpdateTask(task)
        ? resolveSelfUpdateWorkspace(this.config.workspaceRoot, task)
        : resolveTaskWorkspace(this.config.workspaceRoot, task);
    } catch (error) {
      const message = errorMessage(error);
      this.output(`Rejected ${task.task_id}: ${message}`);
      await this.reportClaimFailure(task.task_id, run.run_id, leaseToken, message);
      return false;
    }
    if (isSelfUpdateTask(task)) {
      if (this.config.workerConcurrency !== 1) {
        const message = "Self-update requires AGENT_WORKER_CONCURRENCY=1";
        this.output(`Rejected ${task.task_id}: ${message}`);
        await this.reportClaimFailure(task.task_id, run.run_id, leaseToken, message);
        return false;
      }
      return this.executeSelfUpdate(task, run.run_id, leaseToken, cwd);
    }
    this.output(`Claimed ${task.task_id}: ${task.objective}`);
    await this.hub.updateTask(task.task_id, {
      run_id: run.run_id,
      lease_token: leaseToken,
      status: "working",
      message: "Pi session starting",
    });

    let renewalRunning = false;
    let controlRunning = false;
    let cancelled = false;
    let stopping = false;
    let conversation: Awaited<ReturnType<AgentEngine["createConversation"]>> | undefined;
    const abortConversation = async () => {
      if (conversation?.abort) await conversation.abort();
    };
    const onStop = () => {
      stopping = true;
      void abortConversation();
    };
    signal?.addEventListener("abort", onStop, { once: true });
    if (signal?.aborted) onStop();
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
    }, Math.max(5_000, Math.min(60_000, this.config.leaseSeconds * 1_000 / 3)));

    const controls = setInterval(() => {
      if (controlRunning || stopping || cancelled) return;
      controlRunning = true;
      void this.pollTaskControls(task.task_id, run.run_id, leaseToken, conversation)
        .then(async (status) => {
          if (status === "cancelled") {
            cancelled = true;
            await abortConversation();
          }
        })
        .catch((error: unknown) => {
          this.output(`Task control poll failed: ${errorMessage(error)}`);
        })
        .finally(() => {
          controlRunning = false;
        });
    }, 2_000);

    try {
      conversation = await this.engine.createConversation({
        cwd,
        mode: "remote",
        persisted: true,
      });
      if (stopping) {
        await abortConversation();
        return false;
      }
      if (!conversation.sessionFile) {
        throw new Error("Remote Pi session did not create a persistent file");
      }
      await this.applySessionTitle(task, cwd, conversation);
      this.registry.upsert({
        runId: run.run_id,
        taskId: task.task_id,
        sessionId: conversation.sessionId,
        sessionFile: conversation.sessionFile,
        cwd,
        origin: "remote_task",
        status: "active",
      });
      await this.hub.updateRun(run.run_id, {
        status: "active",
        result: { pi_session_id: conversation.sessionId },
      });
      const result = await conversation.prompt(taskPrompt(task, cwd));
      if (stopping) {
        this.registry.updateStatus(run.run_id, "failed");
        return false;
      }
      const latest = this.hub.getTask
        ? await this.hub.getTask(task.task_id)
        : undefined;
      if (cancelled || latest?.status === "cancelled") {
        this.registry.updateStatus(run.run_id, "cancelled");
        this.output(`Cancelled ${task.task_id}`);
        return false;
      }
      await this.hub.updateTask(task.task_id, {
        run_id: run.run_id,
        lease_token: leaseToken,
        status: "completed",
        message: "Pi session completed",
        result: {
          text: result.text,
          provider: result.provider,
          model: result.model,
          pi_session_id: result.sessionId,
        },
      });
      this.registry.updateStatus(run.run_id, "completed");
      this.output(`Completed ${task.task_id}`);
      return false;
    } catch (error) {
      const message = errorMessage(error);
      if (stopping) {
        this.registry.updateStatus(run.run_id, "failed");
        return false;
      }
      const latest = this.hub.getTask
        ? await this.hub.getTask(task.task_id).catch(() => undefined)
        : undefined;
      if (cancelled || latest?.status === "cancelled") {
        this.registry.updateStatus(run.run_id, "cancelled");
        this.output(`Cancelled ${task.task_id}`);
        return false;
      }
      this.registry.updateStatus(run.run_id, "failed");
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
      clearInterval(controls);
      signal?.removeEventListener("abort", onStop);
      await conversation?.dispose();
    }
  }

  private async reportClaimFailure(
    taskId: string,
    runId: string,
    leaseToken: string,
    message: string,
  ): Promise<void> {
    try {
      await this.hub.updateTask(taskId, {
        run_id: runId,
        lease_token: leaseToken,
        status: "failed",
        message,
        result: {},
      });
    } catch (updateError) {
      this.output(
        `Could not report failure for ${taskId}: ${errorMessage(updateError)}`,
      );
    }
  }

  private async pollTaskControls(
    taskId: string,
    runId: string,
    taskLeaseToken: string,
    conversation: Awaited<ReturnType<AgentEngine["createConversation"]>> | undefined,
  ): Promise<"active" | "cancelled"> {
    if (this.hub.getTask) {
      const task = await this.hub.getTask(taskId);
      if (task.status === "cancelled") return "cancelled";
    }
    if (
      !conversation ||
      !this.hub.claimTaskControls ||
      !this.hub.acknowledgeTaskControl
    ) {
      return "active";
    }
    const controls = await this.hub.claimTaskControls(taskId, {
      run_id: runId,
      lease_token: taskLeaseToken,
    });
    for (const control of controls) {
      if (control.kind === "steer") {
        if (!conversation.steer) throw new Error("Pi session does not support steering");
        await conversation.steer(control.message);
      } else {
        if (!conversation.followUp) {
          throw new Error("Pi session does not support follow-up messages");
        }
        await conversation.followUp(control.message);
      }
      await this.hub.acknowledgeTaskControl(taskId, control.control_id, {
        run_id: runId,
        lease_token: control.lease_token,
      });
      this.output(`Applied ${control.kind} control to ${taskId}`);
    }
    return "active";
  }

  /**
   * Handle a self_update task without an LLM session: pull, reinstall, patch,
   * rebuild, then signal a worker restart. The Hub task is completed before
   * the restart so its result is never lost.
   */
  private async executeSelfUpdate(
    task: HubTask,
    runId: string,
    leaseToken: string,
    cwd: string,
  ): Promise<boolean> {
    this.output(`Self-update claimed ${task.task_id}: ${task.objective}`);
    await this.hub.updateTask(task.task_id, {
      run_id: runId,
      lease_token: leaseToken,
      status: "working",
      message: "Self-update starting",
    });
    let report: SelfUpdateReport;
    try {
      report = runSelfUpdate(this.config, task, cwd);
    } catch (error) {
      const message = errorMessage(error);
      this.output(`Self-update failed: ${message}`);
      await this.hub.updateTask(task.task_id, {
        run_id: runId,
        lease_token: leaseToken,
        status: "failed",
        message: "Self-update failed",
        result: { text: `Self-update failed: ${message}` },
      });
      return false;
    }
    const summary = report.steps.join("\n");
    this.output(
      `Self-update ${report.needsRestart ? "applied" : "already up to date"}`,
    );
    await this.hub.updateTask(task.task_id, {
      run_id: runId,
      lease_token: leaseToken,
      status: "completed",
      message: report.needsRestart
        ? "Self-update applied; worker restarting"
        : "Self-update: already up to date",
      result: {
        text: summary,
        before: report.before,
        after: report.after,
        updated: report.updated,
        needs_restart: report.needsRestart,
        ...(report.pendingInstall
          ? { pending_install: true }
          : {}),
      },
    });
    return report.needsRestart;
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

export function resolveSelfUpdateWorkspace(root: string, task: HubTask): string {
  if (typeof task.input.workspace === "string") {
    return resolveTaskWorkspace(root, task);
  }
  const entrypoint = process.argv[1] ? resolve(process.argv[1]) : "";
  const repository = entrypoint
    ? resolve(dirname(entrypoint), "..", "..", "..")
    : root;
  return resolveTaskWorkspace(root, {
    ...task,
    input: { ...task.input, workspace: relative(root, repository) || "." },
  });
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

function cleanSessionTitle(raw: string): string {
  const singleLine = raw.replace(/\r\n|\r|\n/g, " ").trim();
  // Strip common wrapping like quotes, backticks, or a leading dash/colon.
  const unquoted = singleLine.replace(/^[\s"'`\-*]+|[\s"'`\-*]+$/g, "");
  return unquoted.length > 60 ? `${unquoted.slice(0, 60)}…` : unquoted;
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
