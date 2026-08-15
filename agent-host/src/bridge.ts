import { spawn, type ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import {
  basename,
  dirname,
  isAbsolute,
  join,
  relative as relativePath,
  resolve,
} from "node:path";

import { sanitizedAdapterEnv } from "./child-env.js";
import type { AgentHostConfig } from "./config.js";
import { HubClient } from "./hub-client.js";
import type {
  AdapterArtifact,
  AdapterManifest,
  AdapterResultFile,
  AdapterSessionScope,
  AdapterTaskEnvelope,
} from "./bridge-types.js";
import { AdapterSessionRegistry } from "./adapter-session-registry.js";
import {
  ensureAgentHubProject,
  markCodexSessionVisible,
  registerAgentHubThread,
} from "./codex-project.js";
import type { HubClaim, HubTask } from "./types.js";
import { resolveTaskWorkspace } from "./worker.js";

const CANCEL_POLL_MS = 2_000;
const OUTPUT_TAIL_BYTES = 64 * 1024;
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const SESSION_UUID_RE =
  /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/iu;

export function renderArgs(
  args: string[],
  variables: Record<string, string>,
): string[] {
  return args.map((arg) =>
    arg.replace(/\{([a-z0-9_]+)\}/gu, (match, name: string) => {
      return variables[name] !== undefined ? variables[name] : match;
    }),
  );
}

export function renderEnv(
  env: Record<string, string>,
  variables: Record<string, string>,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(env).map(([key, value]) => [
      key,
      value.replace(/\{([a-z0-9_]+)\}/gu, (match, name: string) => {
        return variables[name] !== undefined ? variables[name] : match;
      }),
    ]),
  );
}

export function writeTaskEnvelope(
  directory: string,
  envelope: AdapterTaskEnvelope,
): string {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const path = join(directory, "AGENT_TASK.json");
  writeJson(path, envelope);
  return path;
}

export function readAdapterResult(filePath: string): AdapterResultFile | undefined {
  if (!existsSync(filePath)) return undefined;
  try {
    const value = JSON.parse(readFileSync(filePath, "utf8")) as unknown;
    return isResultFile(value) ? value : undefined;
  } catch {
    return undefined;
  }
}

export function parseStdoutResult(stdout: string): AdapterResultFile | undefined {
  const trimmed = stdout.trim();
  if (!trimmed) return undefined;
  for (const candidate of [trimmed, lastLine(trimmed)]) {
    try {
      const value = JSON.parse(candidate) as unknown;
      if (isExplicitResult(value)) return value;
    } catch {
      // Try the next candidate.
    }
  }
  return parseJsonlResult(stdout);
}

export function discoverSessionId(cwd: string, glob: string): string | undefined {
  const expanded = glob.startsWith("~/") ? join(homedir(), glob.slice(2)) : glob;
  const patternPath = resolve(cwd, expanded);
  const [staticPrefix, dynamicPattern] = splitGlob(patternPath);
  if (!dynamicPattern.includes("*") || !existsSync(staticPrefix)) {
    return undefined;
  }
  const matcher = new RegExp(
    `^${dynamicPattern
      .split("*")
      .map((part) => part.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&"))
      .join(".*")}$`,
    "u",
  );
  const found: Array<{ path: string; entry: string; modified: number }> = [];
  const visit = (directory: string): void => {
    for (const entry of readdirSync(directory)) {
      const path = join(directory, entry);
      let modified = 0;
      try {
        const stats = statSync(path);
        modified = stats.mtimeMs;
        if (stats.isDirectory()) {
          visit(path);
          continue;
        }
      } catch {
        continue;
      }
      const relative = path.slice(staticPrefix.length).replace(/^[/\\]+/u, "");
      if (matcher.test(relative)) found.push({ path, entry: relative, modified });
    }
  };
  visit(staticPrefix);
  const newest = found.sort((left, right) => right.modified - left.modified)[0];
  if (!newest) return undefined;
  const base = basename(newest.path).replace(/\.jsonl?$/u, "");
  const uuid = base.match(SESSION_UUID_RE)?.[1];
  return uuid ?? base;
}

export { sanitizedAdapterEnv } from "./child-env.js";

function splitGlob(patternPath: string): [string, string] {  const marker = patternPath.indexOf("**");
  if (marker < 0) {
    return [dirname(patternPath), basename(patternPath)];
  }
  const prefix = patternPath.slice(0, marker).replace(/[/\\]+$/u, "");
  const dynamic = patternPath
    .slice(marker)
    .replace(/^[/\\]+/u, "")
    .replace(/^\*\*\/*/u, "");
  return [prefix || patternPath, dynamic];
}

/**
 * Parse a JSONL event stream (Codex `exec --json` and similar CLIs) into a
 * result object. The last assistant message becomes `text`; the most recent
 * session id found in event metadata becomes `session_id`.
 */
function parseJsonlResult(stdout: string): AdapterResultFile | undefined {
  let text: string | undefined;
  let sessionId: string | undefined;
  let explicit: AdapterResultFile | undefined;
  for (const rawLine of stdout.split(/\r?\n/u)) {
    const line = rawLine.trim();
    if (!line) continue;
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch {
      continue;
    }
    if (typeof value !== "object" || value === null) continue;
    if (isExplicitResult(value)) {
      explicit = value;
      continue;
    }
    const item = value as Record<string, unknown>;
    const foundSessionId = findEventSessionId(item);
    if (foundSessionId) sessionId = foundSessionId;
    const foundText = findAssistantMessageText(item);
    if (foundText !== undefined) text = foundText;
  }
  if (explicit) return explicit;
  if (text === undefined && sessionId === undefined) return undefined;
  return {
    ...(text !== undefined ? { text } : {}),
    ...(sessionId ? { session_id: sessionId } : {}),
  };
}

function findEventSessionId(item: Record<string, unknown>): string | undefined {
  const candidates = [
    item.session_id,
    item.sessionID,
    item.thread_id,
    (item.payload as Record<string, unknown> | undefined)?.session_id,
    (item.payload as Record<string, unknown> | undefined)?.thread_id,
    (item.payload as Record<string, unknown> | undefined)?.response
      ? ((item.payload as Record<string, unknown>).response as Record<string, unknown>)
          ?.session_id
      : undefined,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
  }
  return undefined;
}

function findAssistantMessageText(item: Record<string, unknown>): string | undefined {
  const payload = item.payload as Record<string, unknown> | undefined;
  if (item.type === "text") {
    const part = item.part as Record<string, unknown> | undefined;
    if (
      part &&
      typeof part === "object" &&
      (part.type === "text" || part.type === undefined) &&
      typeof part.text === "string" &&
      part.text.trim()
    ) {
      return part.text.trim();
    }
  }
  if (item.type === "item.completed") {
    const completed = item.item as Record<string, unknown> | undefined;
    if (
      completed &&
      typeof completed === "object" &&
      (completed.type === "agent_message" || completed.type === "message") &&
      typeof completed.text === "string" &&
      completed.text.trim()
    ) {
      return completed.text.trim();
    }
  }
  if (payload && typeof payload === "object") {
    if (
      payload.type === "message" &&
      (payload.role === "assistant" || payload.role === "agent")
    ) {
      const content = payload.content;
      if (Array.isArray(content)) {
        const parts: string[] = [];
        for (const block of content) {
          if (typeof block !== "object" || block === null) continue;
          const entry = block as Record<string, unknown>;
          const blockText = entry.text ?? entry.content;
          if (typeof blockText === "string" && blockText.trim()) {
            parts.push(blockText);
          }
        }
        if (parts.length > 0) return parts.join("\n").trim();
      }
      if (typeof content === "string" && content.trim()) {
        return content.trim();
      }
    }
    if (typeof payload.message === "string" && payload.message.trim()) {
      return payload.message.trim();
    }
  }
  if (typeof item.text === "string" && item.text.trim()) {
    return item.text.trim();
  }
  const rawItem = item.item;
  if (rawItem !== null && typeof rawItem === "object") {
    const nested = rawItem as Record<string, unknown>;
    if (typeof nested.text === "string" && nested.text.trim()) {
      return nested.text.trim();
    }
  }
  return undefined;
}

export class BridgeWorker {
  private readonly sessions: AdapterSessionRegistry;

  constructor(
    private readonly config: AgentHostConfig,
    private readonly hub: HubClient,
    private readonly adapter: AdapterManifest,
    private readonly output: (message: string) => void = console.log,
    private readonly workerSlot = 0,
  ) {
    this.sessions = new AdapterSessionRegistry(config.sessionDir);
    if (adapter.id === "codex") {
      ensureAgentHubProject(config.workspaceRoot);
    }
  }

  async runOnce(waitSeconds = 0, signal?: AbortSignal): Promise<boolean> {
    const claim = await this.hub.claimTask({
      actor_id: this.config.actorId,
      node_id: this.config.nodeId,
      wait_seconds: waitSeconds,
      lease_seconds: this.config.leaseSeconds,
    }, signal);
    if (!claim) return false;
    await this.execute(claim, signal);
    return true;
  }

  async runForever(signal: AbortSignal): Promise<void> {
    this.output(
      `${this.adapter.display_name} bridge ready as ${this.config.actorId} on ${this.config.nodeId} (${this.config.workerSessionMode} sessions)`,
    );
    try {
      while (!signal.aborted) {
        try {
          await this.hub.heartbeat(this.config.nodeId);
          await this.runOnce(this.config.pollSeconds, signal);
        } catch (error) {
          if (signal.aborted) return;
          this.output(`Bridge error: ${errorMessage(error)}`);
          await abortableDelay(2_000, signal);
        }
      }
    } finally {
      await this.dispose();
    }
  }

  async dispose(): Promise<void> {
    // The adapter process lifecycle is bounded per task; nothing to persist.
  }

  private async execute(
    claim: HubClaim,
    signal?: AbortSignal,
  ): Promise<void> {
    const { task, run, lease_token: leaseToken } = claim;
    let cwd: string;
    try {
      cwd = resolveTaskWorkspace(this.config.workspaceRoot, task);
    } catch (error) {
      const message = errorMessage(error);
      this.output(`Rejected ${task.task_id}: ${message}`);
      await this.reportClaimFailure(task.task_id, run.run_id, leaseToken, message);
      return;
    }

    const taskDir = join(cwd, ".agenthub", run.run_id);
    const continuous =
      this.adapter.session?.resume === true &&
      this.config.workerSessionMode === "continuous";
    const scope = continuous
      ? adapterScope(this.config, this.adapter.id, this.workerSlot, cwd)
      : undefined;
    const resetRequested = task.input.reset_worker_session === true;
    let record = continuous ? this.sessions.get(scope!) : undefined;
    if (record && this.shouldRotate(record)) {
      this.output(`Rotating ${this.adapter.id} session for worker ${this.workerSlot + 1}`);
      record = undefined;
    }
    const sessionId = resetRequested ? undefined : record?.sessionId;

    const envelope: AdapterTaskEnvelope = {
      task_id: task.task_id,
      run_id: run.run_id,
      objective: task.objective,
      input: task.input,
      workspace: cwd,
      capabilities: task.required_capabilities,
      ...(sessionId ? { session_id: sessionId } : {}),
      continue: Boolean(sessionId),
    };
    const envelopePath = writeTaskEnvelope(taskDir, envelope);
    const variables = {
      task_file: envelopePath,
      prompt: task.objective,
      workspace: cwd,
      session_id: sessionId ?? "",
      sandbox:
        process.env.AGENT_ADAPTER_SANDBOX?.trim() || "workspace-write",
      remote_api_key: this.config.remoteApiKey ?? "",
      remote_base_url: this.config.remoteBaseUrl ?? "",
      remote_model: this.config.remoteModel ?? "",
      hub_url: this.config.hubUrl ?? "",
      hub_token: this.config.hubNodeToken ?? this.config.hubToken ?? "",
    };
    const args =
      sessionId && this.adapter.session?.resume_args
        ? renderArgs(this.adapter.session.resume_args, variables)
        : renderArgs(this.adapter.session?.new_args ?? this.adapter.args, variables);

    this.output(`Claimed ${task.task_id}: ${task.objective}`);
    await this.hub.updateTask(task.task_id, {
      run_id: run.run_id,
      lease_token: leaseToken,
      status: "working",
      message: `${this.adapter.display_name} starting`,
    });
    await this.hub.updateRun(run.run_id, {
      status: "active",
      result: {
        adapter: this.adapter.id,
        ...(sessionId ? { session_id: sessionId } : {}),
      },
    });

    let stdoutTail = "";
    let stderrTail = "";
    const child = spawn(this.adapter.command[0]!, [
      ...this.adapter.command.slice(1),
      ...args,
    ], {
      cwd,
      env: {
        ...sanitizedAdapterEnv(process.env),
        ...renderEnv(this.adapter.env ?? {}, variables),
        AGENT_HUB_TASK_FILE: envelopePath,
        AGENT_HUB_WORKSPACE: cwd,
        AGENT_HUB_TASK_ID: task.task_id,
        AGENT_HUB_RUN_ID: run.run_id,
        AGENT_HUB_OBJECTIVE: task.objective,
        AGENT_HUB_SESSION_ID: sessionId ?? "",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout?.on("data", (chunk: Buffer) => {
      stdoutTail = appendTail(stdoutTail, chunk.toString("utf8"));
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      stderrTail = appendTail(stderrTail, chunk.toString("utf8"));
    });

    let stopped = false;
    let timedOut = false;
    let cancelled = false;
    let killed = false;
    const killChild = () => {
      if (killed || child.exitCode !== null) return;
      killed = true;
      child.kill("SIGTERM");
      const graceMs = (this.adapter.cancel_grace_seconds ?? 10) * 1000;
      setTimeout(() => {
        if (child.exitCode === null) child.kill("SIGKILL");
      }, graceMs).unref();
    };
    const onStop = () => {
      stopped = true;
      killChild();
    };
    signal?.addEventListener("abort", onStop, { once: true });
    if (signal?.aborted) onStop();

    let renewalRunning = false;
    const renewal = setInterval(() => {
      if (renewalRunning || stopped) return;
      renewalRunning = true;
      void this.hub
        .updateTask(task.task_id, {
          run_id: run.run_id,
          lease_token: leaseToken,
          status: "working",
          message: `${this.adapter.display_name} active`,
        })
        .catch((error: unknown) => {
          this.output(`Lease renewal failed: ${errorMessage(error)}`);
        })
        .finally(() => {
          renewalRunning = false;
        });
    }, Math.max(5_000, Math.min(60_000, this.config.leaseSeconds * 1_000 / 3)));

    let polling = false;
    const cancelPoll = setInterval(() => {
      if (polling || stopped) return;
      polling = true;
      void this.hub
        .getTask(task.task_id)
        .then((current) => {
          if (current.status === "cancelled") {
            cancelled = true;
            killChild();
          }
        })
        .catch((error: unknown) => {
          this.output(`Task status poll failed: ${errorMessage(error)}`);
        })
        .finally(() => {
          polling = false;
        });
    }, CANCEL_POLL_MS);

    const timeoutMs = (this.adapter.timeout_seconds ?? 3600) * 1000;
    const timeout = setTimeout(() => {
      timedOut = true;
      killChild();
    }, timeoutMs);
    timeout.unref();

    const exitCode = await new Promise<number | null>((done) => {
      child.on("exit", (code) => done(code));
      child.on("error", (error) => {
        this.output(`Failed to spawn ${this.adapter.id}: ${errorMessage(error)}`);
        done(null);
      });
    });
    clearInterval(renewal);
    clearInterval(cancelPoll);
    clearTimeout(timeout);
    signal?.removeEventListener("abort", onStop);

    if (cancelled || stopped) {
      this.output(`Cancelled ${task.task_id}`);
      try {
        await this.hub.updateRun(run.run_id, {
          status: "cancelled",
          result: { adapter: this.adapter.id },
          error: "cancelled by Hub or worker stop",
        });
      } catch (error) {
        this.output(`Could not cancel run: ${errorMessage(error)}`);
      }
      return;
    }

    try {
      const parsed = this.readResult(taskDir, stdoutTail);
      if (timedOut) {
        throw new Error(
          `${this.adapter.display_name} timed out after ${this.adapter.timeout_seconds ?? 3600}s`,
        );
      }
      if (exitCode !== 0) {
        throw new Error(
          trimTail(stderrTail) ||
            `${this.adapter.display_name} exited with code ${String(exitCode)}`,
        );
      }
      if (parsed?.status === "failed") {
        throw new Error(parsed.message || `${this.adapter.display_name} reported failure`);
      }
      const resultField = this.adapter.session?.result_field ?? "session_id";
      const resultSessionId =
        (parsed !== undefined &&
        typeof (parsed as Record<string, unknown>)[resultField] === "string"
          ? ((parsed as Record<string, unknown>)[resultField] as string)
          : undefined) ??
        parsed?.sessionID ??
        (this.adapter.session?.discovery_glob
          ? discoverSessionId(cwd, this.adapter.session.discovery_glob)
          : undefined);
      if (continuous && typeof resultSessionId === "string" && resultSessionId) {
        this.sessions.upsert(scope!, resultSessionId, task.task_id, {
          reset: resetRequested,
        });
      }
      if (
        this.adapter.id === "codex" &&
        typeof resultSessionId === "string" &&
        resultSessionId
      ) {
        registerAgentHubThread(resultSessionId, cwd);
        markCodexSessionVisible(resultSessionId);
      }
      const text =
        (typeof parsed?.text === "string" && parsed.text.trim()
          ? parsed.text
          : undefined) ??
        (trimTail(stdoutTail) || "");
      await this.uploadArtifacts(parsed?.artifacts, task, run.run_id, cwd);
      await this.hub.updateTask(task.task_id, {
        run_id: run.run_id,
        lease_token: leaseToken,
        status: "completed",
        message: parsed?.message ?? `${this.adapter.display_name} completed`,
        result: {
          text,
          ...(parsed?.result ?? {}),
          adapter: this.adapter.id,
          ...(typeof resultSessionId === "string" && resultSessionId
            ? { session_id: resultSessionId }
          : {}),
        },
      });
      this.output(`Completed ${task.task_id}`);
    } catch (error) {
      const message = errorMessage(error);
      this.output(`Failed ${task.task_id}: ${message}`);
      try {
        await this.hub.updateTask(task.task_id, {
          run_id: run.run_id,
          lease_token: leaseToken,
          status: "failed",
          message,
          result: {},
        });
      } catch (updateError) {
        this.output(`Could not report failure: ${errorMessage(updateError)}`);
      }
      throw error;
    }
  }

  private readResult(
    taskDir: string,
    stdout: string,
  ): AdapterResultFile | undefined {
    if (this.adapter.result_mode === "file") {
      return readAdapterResult(join(taskDir, "AGENT_RESULT.json"));
    }
    return parseStdoutResult(stdout);
  }

  private async uploadArtifacts(
    artifacts: AdapterArtifact[] | undefined,
    task: HubTask,
    runId: string,
    cwd: string,
  ): Promise<void> {
    for (const artifact of artifacts ?? []) {
      try {
        const path = resolve(cwd, artifact.path);
        const relative = relativePath(cwd, path);
        if (
          relative === "" ||
          relative.startsWith("..") ||
          isAbsolute(relative)
        ) {
          this.output(
            `Artifact outside the workspace, refusing: ${artifact.path}`,
          );
          continue;
        }
        if (!existsSync(path) || !statSync(path).isFile()) {
          this.output(`Artifact not found, skipping: ${artifact.path}`);
          continue;
        }
        const name = artifact.name ?? basename(path);
        const mediaType = artifact.media_type ?? mediaTypeFor(name);
        const size = statSync(path).size;
        const item: {
          name: string;
          media_type: string;
          task_id: string;
          run_id: string;
          created_by_actor_id: string;
          content_base64?: string;
          uri?: string;
          sha256?: string;
          size_bytes?: number;
        } = {
          name,
          media_type: mediaType,
          task_id: task.task_id,
          run_id: runId,
          created_by_actor_id: this.config.actorId,
        };
        if (size <= MAX_UPLOAD_BYTES) {
          item.content_base64 = readFileSync(path).toString("base64");
        } else {
          item.uri = `file://${path}`;
          item.size_bytes = size;
          item.sha256 = createHash("sha256").update(readFileSync(path)).digest("hex");
        }
        try {
          await this.hub.addArtifact(item);
        } catch (uploadError) {
          this.output(
            `Artifact content upload failed, registering metadata only: ${errorMessage(uploadError)}`,
          );
          delete item.content_base64;
          item.uri = `file://${path}`;
          item.size_bytes = size;
          item.sha256 = createHash("sha256").update(readFileSync(path)).digest("hex");
          await this.hub.addArtifact(item);
        }
      } catch (error) {
        this.output(`Artifact upload failed for ${artifact.path}: ${errorMessage(error)}`);
      }
    }
  }

  private shouldRotate(record: {
    taskCount: number;
    updatedAt: string;
  }): boolean {
    if (
      this.config.workerSessionMaxTasks > 0 &&
      record.taskCount >= this.config.workerSessionMaxTasks
    ) {
      return true;
    }
    if (this.config.workerSessionMaxAgeHours > 0) {
      const ageMs = Date.now() - Date.parse(record.updatedAt);
      if (ageMs > this.config.workerSessionMaxAgeHours * 3_600_000) {
        return true;
      }
    }
    return false;
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
    } catch (error) {
      this.output(`Could not report claim failure: ${errorMessage(error)}`);
    }
  }
}

function adapterScope(
  config: AgentHostConfig,
  adapterId: string,
  workerSlot: number,
  cwd: string,
): AdapterSessionScope {
  return {
    adapterId,
    actorId: config.actorId,
    nodeId: config.nodeId,
    principalId: config.principalId,
    workerSlot,
    cwd,
  };
}

function writeJson(path: string, value: unknown): void {
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  chmodSync(path, 0o600);
}

function appendTail(current: string, chunk: string): string {
  const combined = current + chunk;
  return combined.length > OUTPUT_TAIL_BYTES
    ? combined.slice(combined.length - OUTPUT_TAIL_BYTES)
    : combined;
}

function trimTail(value: string): string {
  return value.trim();
}

function lastLine(value: string): string {
  const lines = value.split(/\r?\n/u).filter((line) => line.trim());
  return lines[lines.length - 1] ?? "";
}

function mediaTypeFor(name: string): string {
  const extension = name.includes(".") ? name.split(".").pop()?.toLowerCase() : "";
  const byExtension: Record<string, string> = {
    json: "application/json",
    md: "text/markdown",
    txt: "text/plain",
    log: "text/plain",
    csv: "text/csv",
    png: "image/png",
    jpg: "image/jpeg",
    pdf: "application/pdf",
    zip: "application/zip",
  };
  return (extension && byExtension[extension]) || "application/octet-stream";
}

function isResultFile(value: unknown): value is AdapterResultFile {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  if (item.status !== undefined && item.status !== "completed" && item.status !== "failed") {
    return false;
  }
  return true;
}

function isExplicitResult(value: unknown): value is AdapterResultFile {
  if (typeof value !== "object" || value === null) return false;
  const status = (value as Record<string, unknown>).status;
  return status === "completed" || status === "failed";
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
