import { spawn, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface, type Interface } from "node:readline";

import { sanitizedChildEnv } from "./child-env.js";
import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";
import type {
  AgentConversation,
  AgentEngine,
  AgentResult,
  AgentSessionPosition,
  AgentTaskContext,
} from "./types.js";

const REQUEST_TIMEOUT_MS = 30_000;
const SHUTDOWN_TIMEOUT_MS = 1_000;
const EOF_GRACE_MS = 6_000;
const SIGTERM_GRACE_MS = 3_000;
const STDERR_TAIL_LIMIT = 4_000;

interface JsonRpcMessage {
  jsonrpc?: string;
  id?: number;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: { code?: number; message?: string; data?: unknown };
}

interface DshRuntimeNotification {
  method: string;
  params: Record<string, unknown>;
}

interface DshPromptReceipt {
  messageId: string;
}

interface PendingRequest {
  resolve(message: JsonRpcMessage): void;
  reject(error: Error): void;
  timeout: ReturnType<typeof setTimeout>;
}

export class DshRuntimeClosedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DshRuntimeClosedError";
  }
}

class DshProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DshProtocolError";
  }
}

class DshRuntime {
  private child: ChildProcess | undefined;
  private readline: Interface | undefined;
  private nextId = 1;
  private readonly pending = new Map<number, PendingRequest>();
  private stderrTail = "";
  private initialized = false;
  private closed = false;

  constructor(
    private readonly options: {
      command: string[];
      cwd: string;
      env: NodeJS.ProcessEnv;
      onNotification: (notification: DshRuntimeNotification) => void;
      onExit: (error: DshRuntimeClosedError) => void;
    },
  ) {}

  start(): void {
    if (this.child) return;
    if (this.closed) {
      throw new DshRuntimeClosedError("DeepSeek Harness runtime is closed");
    }
    const child = spawn(
      this.options.command[0]!,
      this.options.command.slice(1),
      {
        cwd: this.options.cwd,
        env: {
          ...sanitizedChildEnv(process.env),
          ...this.options.env,
        },
        stdio: ["pipe", "pipe", "pipe"],
      },
    );
    this.child = child;
    this.readline = createInterface({ input: child.stdout! });
    this.readline.on("line", (line) => this.handleLine(line));
    child.stderr?.on("data", (chunk: Buffer) => {
      this.stderrTail = tail(this.stderrTail + chunk.toString("utf8"));
    });
    child.once("error", (error) => {
      this.failAll(
        new DshRuntimeClosedError(
          `Could not start DeepSeek Harness runtime: ${message(error)}`,
        ),
      );
    });
    child.once("exit", (code, signal) => {
      const detail = this.stderrTail.trim();
      const failure = new DshRuntimeClosedError(
        `DeepSeek Harness runtime exited (code ${String(code)}, signal ${String(signal)})${detail ? `: ${detail}` : ""}`,
      );
      this.failAll(failure);
      this.readline?.close();
      this.readline = undefined;
      this.child = undefined;
      this.options.onExit(failure);
    });
  }

  async initialize(params: Record<string, unknown>): Promise<void> {
    if (this.initialized) return;
    this.start();
    const response = await this.request("initialize", params, REQUEST_TIMEOUT_MS);
    const serverInfo = readRecord(response.result).serverInfo;
    if (
      !isRecord(serverInfo) ||
      serverInfo.name !== "deepseek-harness-sdk-runtime"
    ) {
      throw new DshProtocolError(
        `Unexpected DeepSeek Harness runtime identity: ${JSON.stringify(serverInfo)}`,
      );
    }
    this.initialized = true;
  }

  async prompt(
    sessionId: string,
    text: string,
  ): Promise<DshPromptReceipt> {
    const response = await this.request(
      "session/prompt",
      {
        sessionId,
        contentBlocks: [{ type: "text", text }],
      },
      REQUEST_TIMEOUT_MS,
    );
    const result = readRecord(response.result);
    if (typeof result.messageId !== "string" || !result.messageId) {
      throw new DshProtocolError(
        `session/prompt returned no messageId: ${JSON.stringify(response.result)}`,
      );
    }
    return { messageId: result.messageId };
  }

  request(
    method: string,
    params: Record<string, unknown> | undefined,
    timeoutMs = REQUEST_TIMEOUT_MS,
  ): Promise<JsonRpcMessage> {
    this.start();
    const id = this.nextId;
    this.nextId += 1;
    const payload = JSON.stringify({
      jsonrpc: "2.0",
      id,
      method,
      ...(params === undefined ? {} : { params }),
    });
    return new Promise<JsonRpcMessage>((resolvePromise, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`DeepSeek Harness request ${method} timed out`));
      }, timeoutMs);
      this.pending.set(id, { resolve: resolvePromise, reject, timeout });
      this.child!.stdin!.write(`${payload}\n`, "utf8", (error) => {
        if (!error) return;
        this.pending.delete(id);
        clearTimeout(timeout);
        reject(
          new DshRuntimeClosedError(
            `Could not write to DeepSeek Harness runtime: ${message(error)}`,
          ),
        );
      });
    });
  }

  private handleLine(line: string): void {
    let parsed: JsonRpcMessage;
    try {
      parsed = JSON.parse(line) as JsonRpcMessage;
    } catch {
      this.failAll(
        new DshProtocolError(
          `DeepSeek Harness runtime wrote invalid JSON to stdout: ${line.slice(0, 200)}`,
        ),
      );
      return;
    }
    if (parsed.id !== undefined && this.pending.has(parsed.id)) {
      const pending = this.pending.get(parsed.id)!;
      this.pending.delete(parsed.id);
      clearTimeout(pending.timeout);
      if (parsed.error) {
        pending.reject(
          new DshProtocolError(
            `DeepSeek Harness request failed (${String(parsed.error.code ?? "?")}): ${parsed.error.message ?? "unknown error"}`,
          ),
        );
      } else {
        pending.resolve(parsed);
      }
      return;
    }
    if (typeof parsed.method === "string") {
      this.options.onNotification({
        method: parsed.method,
        params: isRecord(parsed.params) ? parsed.params : {},
      });
    }
  }

  private failAll(error: Error): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timeout);
      pending.reject(error);
    }
    this.pending.clear();
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    const child = this.child;
    if (!child || child.exitCode !== null || child.signalCode !== null) return;
    try {
      await this.request("shutdown", undefined, SHUTDOWN_TIMEOUT_MS);
    } catch {
      // The EOF/SIGTERM/SIGKILL ladder below is authoritative.
    }
    child.stdin?.end();
    await waitForExit(child, EOF_GRACE_MS);
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      await waitForExit(child, SIGTERM_GRACE_MS);
    }
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGKILL");
      await waitForExit(child, SIGTERM_GRACE_MS);
    }
  }

  kill(): void {
    const child = this.child;
    if (child && child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) {
          child.kill("SIGKILL");
        }
      }, SIGTERM_GRACE_MS).unref();
    }
    this.failAll(
      new DshRuntimeClosedError("DeepSeek Harness runtime was stopped"),
    );
  }
}

interface Waiter {
  cursor: number;
  process(notification: DshRuntimeNotification): boolean;
  resolve(): void;
  reject(error: Error): void;
}

class DshSessionChannel {
  private readonly notifications: DshRuntimeNotification[] = [];
  private readonly waiters = new Set<Waiter>();
  private failure: Error | undefined;

  get length(): number {
    return this.notifications.length;
  }

  push(notification: DshRuntimeNotification): void {
    if (this.failure) return;
    this.notifications.push(notification);
    this.settle();
  }

  waitFrom(
    cursor: number,
    process: (notification: DshRuntimeNotification) => boolean,
  ): Promise<void> {
    if (this.failure) return Promise.reject(this.failure);
    return new Promise<void>((resolvePromise, reject) => {
      const waiter: Waiter = {
        cursor,
        process,
        resolve: resolvePromise,
        reject,
      };
      this.waiters.add(waiter);
      this.settle();
    });
  }

  fail(error: Error): void {
    this.failure ??= error;
    for (const waiter of [...this.waiters]) {
      this.waiters.delete(waiter);
      waiter.reject(this.failure);
    }
  }

  private settle(): void {
    for (const waiter of [...this.waiters]) {
      while (waiter.cursor < this.notifications.length) {
        const notification = this.notifications[waiter.cursor]!;
        waiter.cursor += 1;
        let complete = false;
        try {
          complete = waiter.process(notification);
        } catch (error) {
          this.waiters.delete(waiter);
          waiter.reject(error instanceof Error ? error : new Error(String(error)));
          return;
        }
        if (complete) {
          this.waiters.delete(waiter);
          waiter.resolve();
          break;
        }
      }
    }
  }
}

class DshConversation implements AgentConversation {
  private usable = true;
  private entryCount = 0;
  private messageCount = 0;
  private lastAssistantText: string | undefined;
  private turnError: string | undefined;
  private sessionName = "";
  private readonly channel = new DshSessionChannel();

  constructor(
    private readonly engine: DshAgentEngine,
    readonly sessionId: string,
    readonly sessionFile: string,
    readonly cwd: string,
  ) {}

  get isUsable(): boolean {
    return this.usable;
  }

  async prompt(
    text: string,
    onText?: (delta: string) => void,
  ): Promise<AgentResult> {
    if (!this.usable) {
      throw new Error(
        `DeepSeek Harness session ${this.sessionId} is no longer usable`,
      );
    }
    const runtime = await this.engine.runtimeFor(this.cwd);
    this.turnError = undefined;
    const cursor = this.channel.length;
    const receipt = await runtime.prompt(this.sessionId, text);
    let received = false;
    let idleAfterReceipt = false;
    await this.channel.waitFrom(cursor, (notification) => {
      if (notification.method === "session.event") {
        if (isInboxReceipt(notification, receipt.messageId)) {
          received = true;
        }
        const chunk = textDelta(notification);
        if (chunk !== undefined) onText?.(chunk);
        return false;
      }
      if (notification.method === "session.status") {
        if (notification.params.status === "running") {
          idleAfterReceipt = false;
          return false;
        }
        if (notification.params.status === "idle") {
          idleAfterReceipt = received;
          return idleAfterReceipt;
        }
      }
      return false;
    });
    if (this.turnError) throw new Error(this.turnError);
    const finalText = (this.lastAssistantText ?? "").trim();
    if (!finalText) {
      throw new Error(
        "DeepSeek Harness session ended without an assistant message",
      );
    }
    return {
      text: finalText,
      provider: this.engine.providerName,
      model: this.engine.modelName,
      sessionId: this.sessionId,
    };
  }

  async steer(text: string): Promise<void> {
    await this.queueControl(text);
  }

  async followUp(text: string): Promise<void> {
    await this.queueControl(text);
  }

  async abort(): Promise<void> {
    if (!this.usable) return;
    this.engine.abortRuntime(this.cwd);
  }

  getSessionPosition(): AgentSessionPosition {
    return { entryCount: this.entryCount, messageCount: this.messageCount };
  }

  setTaskContext(_context?: AgentTaskContext): void {
    // The dsh JSON-RPC protocol has no custom durable task-boundary event.
  }

  setSessionName(name: string): void {
    this.sessionName = name;
    this.writeMarker();
  }

  async dispose(): Promise<void> {
    if (!this.usable) return;
    this.usable = false;
    this.engine.releaseSession(this);
  }

  deliver(notification: DshRuntimeNotification): void {
    const event = notification.params.event;
    if (notification.method === "session.event") {
      this.entryCount += 1;
      if (isRecord(event) && typeof event.type === "string") {
        if (
          event.type === "user/message" ||
          event.type === "assistant/message"
        ) {
          this.messageCount += 1;
        }
        if (event.type === "assistant/message") {
          const extracted = assistantText(event);
          if (extracted !== undefined) this.lastAssistantText = extracted;
        }
        if (event.type === "turn/end") {
          const reason = isRecord(event.data)
            ? (event.data.reason as Record<string, unknown> | undefined)
            : undefined;
          if (
            isRecord(reason) &&
            reason.kind === "error" &&
            isRecord(reason.error) &&
            typeof reason.error.message === "string"
          ) {
            this.turnError = reason.error.message;
          }
        }
      }
    }
    this.channel.push(notification);
  }

  markUnusable(error: Error): void {
    this.usable = false;
    this.channel.fail(error);
  }

  private async queueControl(text: string): Promise<void> {
    if (!this.usable) {
      throw new Error(
        `DeepSeek Harness session ${this.sessionId} is no longer usable`,
      );
    }
    const runtime = await this.engine.runtimeFor(this.cwd);
    const cursor = this.channel.length;
    const receipt = await runtime.prompt(this.sessionId, text);
    await this.channel.waitFrom(cursor, (notification) =>
      isInboxReceipt(notification, receipt.messageId),
    );
  }

  private writeMarker(): void {
    writeFileSync(
      this.sessionFile,
      `${JSON.stringify(
        {
          version: 1,
          sessionId: this.sessionId,
          sessionName: this.sessionName,
          updatedAt: new Date().toISOString(),
        },
        null,
        2,
      )}\n`,
      { encoding: "utf8", mode: 0o600 },
    );
  }
}

export class DshAgentEngine implements AgentEngine {
  private readonly runtimes = new Map<string, DshRuntime>();
  private readonly conversations = new Set<DshConversation>();
  private readonly sessionsByCwd = new Map<string, Set<DshConversation>>();
  private disposed = false;

  private constructor(
    private readonly config: AgentHostConfig,
    private readonly hub?: HubClient,
  ) {}

  static async create(
    config: AgentHostConfig,
    hub?: HubClient,
  ): Promise<DshAgentEngine> {
    const configPath = config.dshConfigPath ?? defaultDshConfigPath();
    if (!existsSync(configPath)) {
      throw new Error(
        `DeepSeek Harness worker config not found: ${configPath}. Set AGENT_DSH_CONFIG or run dsh-jsonrpc-agent with its own cordis.yml.`,
      );
    }
    return new DshAgentEngine(config, hub);
  }

  async createConversation(options: {
    cwd: string;
    mode: "local" | "remote" | "diagnostic";
    persisted: boolean;
    sessionFile?: string;
    subagentDepth?: number;
  }): Promise<AgentConversation> {
    if (this.disposed) {
      throw new Error("DeepSeek Harness engine is disposed");
    }
    if (options.mode === "local") {
      throw new Error(
        "DeepSeek Harness has no AgentSociety local TUI. Use `./agent dsh-dispatch` or `dsh web` for interactive sessions.",
      );
    }
    if (options.sessionFile) {
      throw new Error(
        "DeepSeek Harness SDK sessions cannot be resumed across process restarts",
      );
    }
    const cwd = resolve(options.cwd);
    const sessionId = `dsh-${randomUUID().replaceAll("-", "")}`;
    const sessionFile = resolve(
      this.sessionRoot(),
      `${sessionId}.agent-society.json`,
    );
    mkdirSync(this.sessionRoot(), { recursive: true, mode: 0o700 });
    const conversation = new DshConversation(
      this,
      sessionId,
      sessionFile,
      cwd,
    );
    conversation.setSessionName("DeepSeek Harness worker session");
    this.conversations.add(conversation);
    const cwdSessions =
      this.sessionsByCwd.get(cwd) ?? new Set<DshConversation>();
    cwdSessions.add(conversation);
    this.sessionsByCwd.set(cwd, cwdSessions);
    return conversation;
  }

  async runtimeFor(cwd: string): Promise<DshRuntime> {
    const existing = this.runtimes.get(cwd);
    if (existing) return existing;
    const runtime = this.createRuntime(cwd);
    this.runtimes.set(cwd, runtime);
    try {
      await runtime.initialize({
        cwd,
        provider: this.providerName,
        model: this.modelName,
        maxTokens: this.config.dshMaxTokens,
      });
      return runtime;
    } catch (error) {
      this.runtimes.delete(cwd);
      await runtime.close();
      throw error;
    }
  }

  releaseSession(conversation: DshConversation): void {
    this.conversations.delete(conversation);
    const cwdSessions = this.sessionsByCwd.get(conversation.cwd);
    cwdSessions?.delete(conversation);
    if (cwdSessions && cwdSessions.size === 0) {
      this.sessionsByCwd.delete(conversation.cwd);
      if (this.config.workerSessionMode === "per_task") {
        const runtime = this.runtimes.get(conversation.cwd);
        if (runtime) {
          this.runtimes.delete(conversation.cwd);
          void runtime.close();
        }
      }
    }
  }

  abortRuntime(cwd: string): void {
    const runtime = this.runtimes.get(cwd);
    if (runtime) {
      this.runtimes.delete(cwd);
      runtime.kill();
    }
    const sessions = this.sessionsByCwd.get(cwd);
    if (sessions) {
      for (const conversation of [...sessions]) {
        conversation.markUnusable(
          new Error("DeepSeek Harness runtime was aborted"),
        );
      }
    }
  }

  async dispose(): Promise<void> {
    this.disposed = true;
    const runtimes = [...this.runtimes.values()];
    this.runtimes.clear();
    await Promise.all(runtimes.map((runtime) => runtime.close()));
    for (const conversation of [...this.conversations]) {
      conversation.markUnusable(new Error("DeepSeek Harness engine disposed"));
    }
    this.conversations.clear();
    this.sessionsByCwd.clear();
  }

  get providerName(): string {
    return this.config.dshProvider ?? "deepseek-official";
  }

  get modelName(): string {
    return this.config.dshModel ?? "deepseek-v4-flash";
  }

  private createRuntime(cwd: string): DshRuntime {
    const configPath = this.config.dshConfigPath ?? defaultDshConfigPath();
    const command = [
      this.config.dshRuntimeBin ?? "dsh-jsonrpc-agent",
      ...(this.config.dshRuntimeArgs ?? []),
      configPath,
    ];
    const env: NodeJS.ProcessEnv = {
      DSH_CWD: cwd,
      DSH_MODEL: this.modelName,
      DSH_CONTEXT_WINDOW: String(this.config.contextWindow),
      DSH_MAX_TOKENS: String(this.config.dshMaxTokens),
      DSH_PERMISSION_MODE: this.config.dshPermissionMode,
      DSH_TOOL_POLICY: this.config.remoteToolPolicy,
      DSH_REASONING_EFFORT: reasoningEffort(this.config.thinkingLevel),
      DSH_THINKING:
        this.config.thinkingLevel === "off" ? "disabled" : "enabled",
      DSH_SESSION_ROOT: this.sessionRoot(),
      AGENT_SOCIETY_HUB_ENABLED:
        this.hub && this.config.dshHubMcp ? "1" : "0",
      AGENT_SOCIETY_WEB_SEARCH: this.config.dshWebSearch ? "1" : "0",
      ...(this.hub && this.config.dshHubMcp && this.config.hubUrl
        ? {
            AGENT_SOCIETY_HUB_URL: this.config.hubUrl,
            AGENT_SOCIETY_HUB_MCP_TOKEN: this.hub.nodeToken,
          }
        : {}),
      ...(this.config.remoteApiKey
        ? { DEEPSEEK_API_KEY: this.config.remoteApiKey }
        : {}),
      ...(this.config.remoteBaseUrl
        ? { DEEPSEEK_BASE_URL: this.config.remoteBaseUrl }
        : {}),
    };
    return new DshRuntime({
      command,
      cwd,
      env,
      onNotification: (notification) => this.dispatch(notification),
      onExit: (error) => {
        this.runtimes.delete(cwd);
        const sessions = this.sessionsByCwd.get(cwd);
        if (sessions) {
          for (const conversation of [...sessions]) {
            conversation.markUnusable(error);
          }
        }
      },
    });
  }

  private dispatch(notification: DshRuntimeNotification): void {
    const sessionId = notification.params.sessionId;
    if (typeof sessionId !== "string") return;
    for (const conversation of this.conversations) {
      if (conversation.sessionId === sessionId) {
        conversation.deliver(notification);
        return;
      }
    }
  }

  private sessionRoot(): string {
    return (
      this.config.dshSessionRoot ??
      resolve(this.config.sessionDir, "dsh-sessions")
    );
  }
}

function defaultDshConfigPath(): string {
  return fileURLToPath(
    new URL("../../config/dsh-worker.cordis.yml", import.meta.url),
  );
}

function reasoningEffort(level: string): string {
  if (level === "off") return "off";
  if (level === "max" || level === "xhigh") return "max";
  return "high";
}

function isInboxReceipt(
  notification: DshRuntimeNotification,
  messageId: string,
): boolean {
  const event = notification.params.event;
  if (!isRecord(event) || event.type !== "agent/inbox/spliced") return false;
  const inserted = isRecord(event.data) ? event.data.inserted : undefined;
  if (!Array.isArray(inserted)) return false;
  return inserted.some(
    (entry) => isRecord(entry) && entry.id === messageId,
  );
}

function textDelta(
  notification: DshRuntimeNotification,
): string | undefined {
  const event = notification.params.event;
  if (!isRecord(event) || event.type !== "assistant/chunk") return undefined;
  const chunk = isRecord(event.data) ? event.data.chunk : undefined;
  if (!isRecord(chunk) || chunk.type !== "text-delta") return undefined;
  return typeof chunk.text === "string" ? chunk.text : undefined;
}

function assistantText(event: Record<string, unknown>): string | undefined {
  const data = event.data;
  if (!isRecord(data) || !isRecord(data.message)) return undefined;
  const content = data.message.content;
  if (!Array.isArray(content)) return undefined;
  const parts: string[] = [];
  for (const block of content) {
    if (
      isRecord(block) &&
      block.type === "text" &&
      typeof block.text === "string"
    ) {
      parts.push(block.text);
    }
  }
  return parts.join("").trim();
}

function readRecord(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new DshProtocolError(
      `Expected a JSON object, got ${JSON.stringify(value)}`,
    );
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function tail(value: string): string {
  return value.length > STDERR_TAIL_LIMIT
    ? value.slice(value.length - STDERR_TAIL_LIMIT)
    : value;
}

function waitForExit(
  child: ChildProcess,
  graceMs: number,
): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve();
  }
  return new Promise<void>((done) => {
    const timer = setTimeout(done, graceMs);
    timer.unref();
    child.once("exit", () => {
      clearTimeout(timer);
      done();
    });
  });
}
