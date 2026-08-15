import { existsSync, statSync } from "node:fs";
import { basename, dirname, relative, resolve } from "node:path";

import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";
import { RunSessionRegistry } from "./run-registry.js";
import {
  isSelfUpdateTask,
  restartWorker,
  runSelfUpdate,
  type SelfUpdateReport,
} from "./self-update.js";
import {
  PI_ENGINE_PROFILE,
  type AgentEngine,
  type AgentEngineProfile,
  type AgentSessionPosition,
  type HubClaim,
  type HubTask,
} from "./types.js";
import {
  WorkerSessionRegistry,
  type WorkerSessionRecord,
  type WorkerSessionScope,
} from "./worker-session-registry.js";

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
  private readonly workerSessions: WorkerSessionRegistry;
  private activeContinuous:
    | {
        key: string;
        conversation: Awaited<ReturnType<AgentEngine["createConversation"]>>;
        record: WorkerSessionRecord;
      }
    | undefined;

  constructor(
    private readonly config: AgentHostConfig,
    private readonly hub: WorkerHub,
    private readonly engine: AgentEngine,
    private readonly output: (message: string) => void = console.log,
    private readonly restart: () => void = () => {
      restartWorker(config);
      process.exit(0);
    },
    private readonly workerSlot = 0,
    private readonly engineProfile: AgentEngineProfile = PI_ENGINE_PROFILE,
  ) {
    this.registry = new RunSessionRegistry(config.sessionDir);
    this.workerSessions = new WorkerSessionRegistry(config.sessionDir);
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
      await this.dispose();
      this.restart();
    }
    return true;
  }

  async runForever(signal: AbortSignal): Promise<void> {
    this.output(
      `${this.engineProfile.label} worker ${this.workerSlot + 1} ready as ${this.config.actorId} on ${this.config.nodeId} (${this.config.workerSessionMode} sessions)`,
    );
    try {
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
    } finally {
      await this.dispose();
    }
  }

  async dispose(): Promise<void> {
    const active = this.activeContinuous;
    this.activeContinuous = undefined;
    await active?.conversation.dispose();
  }

  private async applySessionTitle(
    task: HubTask,
    cwd: string,
    conversation: Awaited<ReturnType<AgentEngine["createConversation"]>>,
  ): Promise<void> {
    if (!this.engineProfile.generateSessionTitles) return;
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

  private sessionFields(options: {
    conversation: Awaited<ReturnType<AgentEngine["createConversation"]>>;
    continuous: boolean;
    reused: boolean;
    turnStart?: AgentSessionPosition;
    turnEnd?: AgentSessionPosition;
  }): Record<string, unknown> {
    const prefix = this.engineProfile.sessionFieldPrefix;
    const turnPrefix = prefix.endsWith("_session")
      ? prefix.slice(0, -"_session".length)
      : prefix;
    return {
      [`${prefix}_id`]: options.conversation.sessionId,
      [`${prefix}_mode`]: options.continuous ? "continuous" : "per_task",
      [`${prefix}_reused`]: options.reused,
      ...(options.turnStart
        ? { [`${turnPrefix}_turn_start_entry`]: options.turnStart.entryCount }
        : {}),
      ...(options.turnEnd
        ? { [`${turnPrefix}_turn_end_entry`]: options.turnEnd.entryCount }
        : {}),
    };
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
      message: `${this.engineProfile.label} session starting`,
    });

    let renewalRunning = false;
    let controlRunning = false;
    let cancelled = false;
    let stopping = false;
    let conversation: Awaited<ReturnType<AgentEngine["createConversation"]>> | undefined;
    let continuous = false;
    let sessionReused = false;
    let workerSessionKey: string | undefined;
    let runRecord: ReturnType<RunSessionRegistry["upsert"]> | undefined;
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
          message: `${this.engineProfile.label} session active`,
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
      const acquired = await this.acquireConversation(task, cwd);
      conversation = acquired.conversation;
      continuous = acquired.continuous;
      sessionReused = acquired.reused;
      workerSessionKey = acquired.workerSessionKey;
      if (stopping) {
        await abortConversation();
        return false;
      }
      if (!conversation.sessionFile) {
        throw new Error(
          `Remote ${this.engineProfile.label} session did not create a persistent file`,
        );
      }
      if (!continuous) await this.applySessionTitle(task, cwd, conversation);
      conversation.setTaskContext?.({
        taskId: task.task_id,
        runId: run.run_id,
      });
      const turnStart = conversation.getSessionPosition?.();
      runRecord = this.registry.upsert({
        runId: run.run_id,
        taskId: task.task_id,
        sessionId: conversation.sessionId,
        sessionFile: conversation.sessionFile,
        cwd,
        origin: "remote_task",
        sessionMode: continuous ? "continuous" : "per_task",
        workerSlot: this.workerSlot,
        ...(workerSessionKey ? { workerSessionKey } : {}),
        sessionReused,
        ...(turnStart ? { turnStartEntry: turnStart.entryCount } : {}),
        status: "active",
      });
      await this.hub.updateRun(run.run_id, {
        status: "active",
        result: {
          ...this.sessionFields({
            conversation,
            continuous,
            reused: sessionReused,
            ...(turnStart ? { turnStart } : {}),
          }),
          worker_slot: this.workerSlot,
        },
      });
      const result = await conversation.prompt(
        taskPrompt(task, run.run_id, cwd, continuous),
      );
      if (stopping) {
        this.finishRunRecord(runRecord, conversation, "failed");
        return false;
      }
      const latest = this.hub.getTask
        ? await this.hub.getTask(task.task_id)
        : undefined;
      if (cancelled || latest?.status === "cancelled") {
        this.finishRunRecord(runRecord, conversation, "cancelled");
        this.output(`Cancelled ${task.task_id}`);
        return false;
      }
      const turnEnd = conversation.getSessionPosition?.();
      this.finishRunRecord(runRecord, conversation, "completed");
      await this.hub.updateTask(task.task_id, {
        run_id: run.run_id,
        lease_token: leaseToken,
        status: "completed",
        message: `${this.engineProfile.label} session completed`,
        result: {
          text: result.text,
          provider: result.provider,
          model: result.model,
          ...this.sessionFields({
            conversation,
            continuous,
            reused: sessionReused,
            ...(turnStart ? { turnStart } : {}),
            ...(turnEnd ? { turnEnd } : {}),
          }),
          worker_slot: this.workerSlot,
        },
      });
      this.output(`Completed ${task.task_id}`);
      return false;
    } catch (error) {
      const message = errorMessage(error);
      if (stopping) {
        this.finishRunRecord(runRecord, conversation, "failed");
        return false;
      }
      const latest = this.hub.getTask
        ? await this.hub.getTask(task.task_id).catch(() => undefined)
        : undefined;
      if (cancelled || latest?.status === "cancelled") {
        this.finishRunRecord(runRecord, conversation, "cancelled");
        this.output(`Cancelled ${task.task_id}`);
        return false;
      }
      this.finishRunRecord(runRecord, conversation, "failed");
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
      conversation?.setTaskContext?.();
      if (!continuous) await conversation?.dispose();
    }
  }

  private async acquireConversation(
    task: HubTask,
    cwd: string,
  ): Promise<{
    conversation: Awaited<ReturnType<AgentEngine["createConversation"]>>;
    continuous: boolean;
    reused: boolean;
    workerSessionKey?: string;
  }> {
    if (this.config.workerSessionMode === "per_task") {
      return {
        conversation: await this.engine.createConversation({
          cwd,
          mode: "remote",
          persisted: true,
        }),
        continuous: false,
        reused: false,
      };
    }

    const scope = this.workerSessionScope(task, cwd);
    const key = this.workerSessions.key(scope);
    const resetRequested = task.input.reset_worker_session === true;
    if (
      this.activeContinuous &&
      (this.activeContinuous.key !== key ||
        this.activeContinuous.conversation.isUsable === false ||
        resetRequested ||
        this.shouldRotate(this.activeContinuous.record))
    ) {
      await this.dispose();
    }

    if (
      this.activeContinuous?.key === key &&
      this.activeContinuous.conversation.isUsable !== false
    ) {
      const record = this.workerSessions.upsert(
        scope,
        {
          sessionId: this.activeContinuous.conversation.sessionId,
          sessionFile: requireSessionFile(this.activeContinuous.conversation),
        },
        task.task_id,
      );
      this.activeContinuous.record = record;
      return {
        conversation: this.activeContinuous.conversation,
        continuous: true,
        reused: true,
        workerSessionKey: key,
      };
    }

    let previous = resetRequested ? undefined : this.workerSessions.get(scope);
    if (previous && this.shouldRotate(previous)) {
      this.output(
        `Rotating continuous ${this.engineProfile.label} session ${previous.sessionId} for worker ${this.workerSlot + 1}`,
      );
      previous = undefined;
    }
    let conversation:
      | Awaited<ReturnType<AgentEngine["createConversation"]>>
      | undefined;
    let reused = false;
    if (previous) {
      try {
        conversation = await this.engine.createConversation({
          cwd,
          mode: "remote",
          persisted: true,
          sessionFile: previous.sessionFile,
        });
        reused = true;
        this.output(
          `Resumed continuous ${this.engineProfile.label} session ${conversation.sessionId} for worker ${this.workerSlot + 1}`,
        );
      } catch (error) {
        this.output(
          `Continuous ${this.engineProfile.label} session recovery failed; creating a new session: ${errorMessage(error)}`,
        );
      }
    }
    if (!conversation) {
      conversation = await this.engine.createConversation({
        cwd,
        mode: "remote",
        persisted: true,
      });
      conversation.setSessionName(
        `Worker ${this.workerSlot + 1} · ${basename(cwd) || "workspace"}`,
      );
    }
    const sessionFile = requireSessionFile(conversation);
    const record = this.workerSessions.upsert(
      scope,
      { sessionId: conversation.sessionId, sessionFile },
      task.task_id,
      { reset: resetRequested || !reused },
    );
    this.activeContinuous = { key, conversation, record };
    return {
      conversation,
      continuous: true,
      reused,
      workerSessionKey: key,
    };
  }

  private workerSessionScope(task: HubTask, cwd: string): WorkerSessionScope {
    return {
      actorId: this.config.actorId,
      nodeId: this.config.nodeId,
      principalId: task.principal_id,
      workerSlot: this.workerSlot,
      cwd,
    };
  }

  private shouldRotate(record: WorkerSessionRecord): boolean {
    if (
      this.config.workerSessionMaxTasks > 0 &&
      record.taskCount >= this.config.workerSessionMaxTasks
    ) {
      return true;
    }
    if (this.config.workerSessionMaxAgeHours <= 0) return false;
    const ageMs = Date.now() - Date.parse(record.createdAt);
    return ageMs >= this.config.workerSessionMaxAgeHours * 60 * 60 * 1_000;
  }

  private finishRunRecord(
    record: ReturnType<RunSessionRegistry["upsert"]> | undefined,
    conversation: Awaited<ReturnType<AgentEngine["createConversation"]>> | undefined,
    status: "completed" | "failed" | "cancelled",
  ): void {
    if (!record) return;
    const turnEnd = conversation?.getSessionPosition?.();
    this.registry.upsert({
      ...record,
      status,
      startedAt: record.startedAt,
      ...(turnEnd ? { turnEndEntry: turnEnd.entryCount } : {}),
    });
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
      !this.engineProfile.supportsControls ||
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
        if (!conversation.steer) {
          throw new Error(
            `${this.engineProfile.label} session does not support steering`,
          );
        }
        await conversation.steer(control.message);
      } else {
        if (!conversation.followUp) {
          throw new Error(
            `${this.engineProfile.label} session does not support follow-up messages`,
          );
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

function taskPrompt(
  task: HubTask,
  runId: string,
  cwd: string,
  continuous: boolean,
): string {
  return [
    ...(continuous
      ? [
          "--- BEGIN NEW REMOTE TASK ---",
          "This is a new Hub task in a continuous worker session. Previous task messages are historical context only, not active instructions. Follow only the task boundary below and the user's durable preferences.",
        ]
      : []),
    "You are executing a durable task delegated through the collaboration Hub.",
    `Task ID: ${task.task_id}`,
    `Run ID: ${runId}`,
    `Objective: ${task.objective}`,
    `Configured workspace: ${cwd}`,
    `Structured input: ${JSON.stringify(task.input)}`,
    "Complete the objective with the currently available tools. Return a concise result suitable for the delegating agent. Do not claim actions you did not perform.",
    ...(continuous ? ["--- END NEW REMOTE TASK ENVELOPE ---"] : []),
  ].join("\n");
}

function requireSessionFile(
  conversation: Awaited<ReturnType<AgentEngine["createConversation"]>>,
): string {
  if (!conversation.sessionFile) {
    throw new Error("Remote agent session did not create a persistent file");
  }
  return conversation.sessionFile;
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
