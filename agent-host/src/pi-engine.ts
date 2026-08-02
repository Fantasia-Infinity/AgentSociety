import {
  createAgentSession,
  createAgentSessionFromServices,
  createAgentSessionRuntime,
  createAgentSessionServices,
  defineTool,
  getAgentDir,
  InteractiveMode,
  ModelRuntime,
  SessionManager,
} from "@earendil-works/pi-coding-agent";
import { execFileSync } from "node:child_process";
import { Type } from "typebox";

import { assertRemoteUrl, type AgentHostConfig } from "./config.js";
import { HubClient } from "./hub-client.js";
import type {
  AgentConversation,
  AgentEngine,
  AgentResult,
  TaskStatus,
} from "./types.js";

const HUB_TOOL_NAMES = [
  "hub_list_actors",
  "hub_list_tasks",
  "hub_get_task",
  "hub_create_task",
];

export class PiAgentEngine implements AgentEngine {
  private constructor(
    private readonly config: AgentHostConfig,
    private readonly hub: HubClient,
    private readonly modelRuntime: ModelRuntime,
    private readonly model: NonNullable<
      ReturnType<ModelRuntime["getModel"]>
    >,
  ) {}

  static async create(
    config: AgentHostConfig,
    hub: HubClient,
  ): Promise<PiAgentEngine> {
    const runtime = await ModelRuntime.create();
    let provider: string;
    let modelId: string;

    if (config.piProvider && config.piModel) {
      provider = config.piProvider;
      modelId = config.piModel;
    } else {
      provider = "ssh-remote";
      modelId = config.remoteModel!;
      const apiKey = remoteApiKey(config);
      runtime.registerProvider(provider, {
        name: "SSH remote OpenAI-compatible API",
        baseUrl: config.remoteBaseUrl!,
        api: "openai-completions",
        ...(apiKey ? { apiKey } : {}),
        models: [
          {
            id: modelId,
            name: modelId,
            api: "openai-completions",
            reasoning: false,
            input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: config.contextWindow,
            maxTokens: config.maxOutputTokens,
          },
        ],
      });
    }

    const model = runtime.getModel(provider, modelId);
    if (!model) {
      throw new Error(`Pi model not found: ${provider}/${modelId}`);
    }
    assertRemoteUrl(model.baseUrl);
    const auth = await runtime.getAuth(model);
    if (!auth) {
      throw new Error(
        `No remote API credential is configured for Pi provider ${provider}`,
      );
    }
    return new PiAgentEngine(config, hub, runtime, model);
  }

  async createConversation(options: {
    cwd: string;
    mode: "local" | "remote";
    persisted: boolean;
  }): Promise<AgentConversation> {
    const customTools = this.createHubTools();
    const tools = this.toolNames(options.mode);
    const sessionManager = options.persisted
      ? SessionManager.create(options.cwd, this.config.sessionDir)
      : SessionManager.inMemory(options.cwd);
    const { session } = await createAgentSession({
      cwd: options.cwd,
      modelRuntime: this.modelRuntime,
      model: this.model,
      tools,
      customTools,
      sessionManager,
      sessionStartEvent: {
        type: "session_start",
        reason: "startup",
      },
    });

    return {
      sessionId: session.sessionId,
      ...(session.sessionFile ? { sessionFile: session.sessionFile } : {}),
      prompt: async (text, onText) => {
        const unsubscribe = session.subscribe((event) => {
          if (
            event.type === "message_update" &&
            event.assistantMessageEvent.type === "text_delta"
          ) {
            onText?.(event.assistantMessageEvent.delta);
          }
        });
        try {
          await session.prompt(text, { source: "rpc" });
          await session.waitForIdle();
          const result = lastAssistantResult(session.messages);
          return {
            ...result,
            sessionId: session.sessionManager.getSessionId(),
          };
        } finally {
          unsubscribe();
        }
      },
      dispose: () => session.dispose(),
    };
  }

  async runTui(options: {
    cwd: string;
    sessionFile?: string;
    initialMessage?: string;
    onSessionReady?: (session: {
      sessionId: string;
      sessionFile?: string;
    }) => void;
  }): Promise<{ sessionId: string; sessionFile?: string; lastText: string }> {
    if (!process.stdin.isTTY || !process.stdout.isTTY) {
      throw new Error("Pi TUI requires an interactive terminal");
    }
    const agentDir = getAgentDir();
    const sessionManager = options.sessionFile
      ? SessionManager.open(
          options.sessionFile,
          this.config.sessionDir,
          options.cwd,
        )
      : SessionManager.create(options.cwd, this.config.sessionDir);
    const createRuntime = async (runtimeOptions: {
      cwd: string;
      agentDir: string;
      sessionManager: SessionManager;
      sessionStartEvent?: {
        type: "session_start";
        reason: "startup" | "reload" | "new" | "resume" | "fork";
        previousSessionFile?: string;
      };
    }) => {
      const services = await createAgentSessionServices({
        cwd: runtimeOptions.cwd,
        agentDir: runtimeOptions.agentDir,
        modelRuntime: this.modelRuntime,
      });
      const created = await createAgentSessionFromServices({
        services,
        sessionManager: runtimeOptions.sessionManager,
        ...(runtimeOptions.sessionStartEvent
          ? { sessionStartEvent: runtimeOptions.sessionStartEvent }
          : {}),
        model: this.model,
        tools: this.toolNames("local"),
        customTools: this.createHubTools(),
      });
      return {
        ...created,
        services,
        diagnostics: services.diagnostics,
      };
    };
    const runtime = await createAgentSessionRuntime(createRuntime, {
      cwd: options.cwd,
      agentDir,
      sessionManager,
      sessionStartEvent: {
        type: "session_start",
        reason: options.sessionFile ? "resume" : "startup",
      },
    });
    const errors = runtime.diagnostics.filter((item) => item.type === "error");
    if (errors.length) {
      await runtime.dispose();
      throw new Error(errors.map((item) => item.message).join("; "));
    }
    options.onSessionReady?.({
      sessionId: runtime.session.sessionId,
      ...(runtime.session.sessionFile
        ? { sessionFile: runtime.session.sessionFile }
        : {}),
    });
    const tui = new InteractiveMode(runtime, {
      ...(options.initialMessage ? { initialMessage: options.initialMessage } : {}),
      verbose: false,
    });
    await tui.run();
    return {
      sessionId: runtime.session.sessionId,
      ...(runtime.session.sessionFile
        ? { sessionFile: runtime.session.sessionFile }
        : {}),
      lastText: optionalLastAssistantText(runtime.session.messages),
    };
  }

  private toolNames(mode: "local" | "remote"): string[] {
    if (mode === "local") {
      return [
        "read",
        "bash",
        "edit",
        "write",
        "grep",
        "find",
        "ls",
        ...HUB_TOOL_NAMES,
      ];
    }
    if (this.config.remoteToolPolicy === "full") {
      return [
        "read",
        "bash",
        "edit",
        "write",
        "grep",
        "find",
        "ls",
        ...HUB_TOOL_NAMES,
      ];
    }
    if (this.config.remoteToolPolicy === "read_only") {
      return ["read", "grep", "find", "ls", ...HUB_TOOL_NAMES];
    }
    return [...HUB_TOOL_NAMES];
  }

  private createHubTools() {
    const listActors = defineTool({
      name: "hub_list_actors",
      label: "List collaboration actors",
      description:
        "List human, agent, and service actors registered in the coordination Hub.",
      parameters: Type.Object({}),
      execute: async () => this.toolResult(await this.hub.listActors()),
    });
    const listTasks = defineTool({
      name: "hub_list_tasks",
      label: "List collaboration tasks",
      description: "List tasks and their current durable status in the Hub.",
      parameters: Type.Object({
        status: Type.Optional(
          Type.Union([
            Type.Literal("submitted"),
            Type.Literal("working"),
            Type.Literal("completed"),
            Type.Literal("failed"),
            Type.Literal("cancelled"),
          ]),
        ),
      }),
      execute: async (_id, params) =>
        this.toolResult(
          await this.hub.listTasks(params.status as TaskStatus | undefined),
        ),
    });
    const getTask = defineTool({
      name: "hub_get_task",
      label: "Get collaboration task",
      description: "Read one task, including its result and artifact references.",
      parameters: Type.Object({ taskId: Type.String({ minLength: 1 }) }),
      execute: async (_id, params) =>
        this.toolResult(await this.hub.getTask(params.taskId)),
    });
    const createTask = defineTool({
      name: "hub_create_task",
      label: "Delegate collaboration task",
      description:
        "Create a durable task for another agent. Use only when delegation helps the user's objective.",
      parameters: Type.Object({
        objective: Type.String({ minLength: 1 }),
        assigneeActorId: Type.Optional(Type.String({ minLength: 1 })),
        requiredCapabilities: Type.Optional(Type.Array(Type.String())),
        input: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
      }),
      execute: async (_id, params) =>
        this.toolResult(
          await this.hub.createTask({
            principal_id: this.config.principalId,
            delegator_actor_id: this.config.actorId,
            objective: params.objective,
            ...(params.assigneeActorId
              ? { assignee_actor_id: params.assigneeActorId }
              : {}),
            ...(params.requiredCapabilities
              ? { required_capabilities: params.requiredCapabilities }
              : {}),
            ...(params.input ? { input: params.input } : {}),
            origin: "agent_tool",
          }),
        ),
    });
    return [listActors, listTasks, getTask, createTask];
  }

  private toolResult(value: unknown) {
    return {
      content: [{ type: "text" as const, text: JSON.stringify(value) }],
      details: {},
    };
  }
}

function remoteApiKey(config: AgentHostConfig): string | undefined {
  if (config.remoteApiKey) return config.remoteApiKey;
  if (!config.remoteApiKeyKeychainService) return undefined;
  if (process.platform !== "darwin") {
    throw new Error("macOS Keychain model credentials require macOS");
  }
  const account = config.remoteApiKeyKeychainAccount;
  const args = ["find-generic-password"];
  if (account) args.push("-a", account);
  args.push("-s", config.remoteApiKeyKeychainService, "-w");
  try {
    return execFileSync("/usr/bin/security", args, {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    throw new Error("Could not load the remote model credential from Keychain");
  }
}

function lastAssistantResult(messages: unknown[]): Omit<AgentResult, "sessionId"> {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!isRecord(message) || message.role !== "assistant") continue;
    if (message.stopReason === "error" || message.stopReason === "aborted") {
      throw new Error(
        typeof message.errorMessage === "string"
          ? message.errorMessage
          : `Pi stopped with ${String(message.stopReason)}`,
      );
    }
    const content = Array.isArray(message.content) ? message.content : [];
    const text = content
      .filter(
        (item): item is Record<string, unknown> =>
          isRecord(item) && item.type === "text",
      )
      .map((item) => String(item.text ?? ""))
      .join("")
      .trim();
    return {
      text,
      provider: String(message.provider ?? "unknown"),
      model: String(message.model ?? "unknown"),
    };
  }
  throw new Error("Pi returned no assistant message");
}

function optionalLastAssistantText(messages: unknown[]): string {
  try {
    return lastAssistantResult(messages).text;
  } catch {
    return "";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
