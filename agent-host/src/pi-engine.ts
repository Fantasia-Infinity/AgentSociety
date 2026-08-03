import {
  createAgentSessionFromServices,
  createAgentSessionRuntime,
  defineTool,
  getAgentDir,
  InteractiveMode,
  ModelRuntime,
  SessionManager,
  type ProjectTrustContext,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import { assertRemoteUrl, type AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";
import {
  activateCompatibleTools,
  collectPiDiagnostics,
  createPiServices,
  sessionToolSelection,
} from "./pi-compatibility.js";
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
    private readonly hub: HubClient | undefined,
    private readonly modelRuntime: ModelRuntime,
    private readonly provider: string,
    private readonly modelId: string,
  ) {}

  static async create(
    config: AgentHostConfig,
    hub?: HubClient,
  ): Promise<PiAgentEngine> {
    const runtime = await ModelRuntime.create();
    let provider: string;
    let modelId: string;

    if (config.piProvider && config.piModel) {
      provider = config.piProvider;
      modelId = config.piModel;
    } else {
      provider = "agent-society-remote";
      modelId = config.remoteModel!;
      const apiKey = config.remoteApiKey;
      runtime.registerProvider(provider, {
        name: "AgentSociety remote OpenAI-compatible API",
        baseUrl: config.remoteBaseUrl!,
        api: "openai-completions",
        ...(apiKey ? { apiKey } : {}),
        models: [
          {
            id: modelId,
            name: modelId,
            api: "openai-completions",
            reasoning: true,
            input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: config.contextWindow,
            maxTokens: config.maxOutputTokens,
            thinkingLevelMap: {
              minimal: null,
              low: null,
              medium: null,
              high: "high",
              max: "max",
            },
          },
        ],
      });
    }

    return new PiAgentEngine(config, hub, runtime, provider, modelId);
  }

  async createConversation(options: {
    cwd: string;
    mode: "local" | "remote" | "diagnostic";
    persisted: boolean;
  }): Promise<AgentConversation> {
    const customTools =
      options.mode === "diagnostic" ? [] : this.createHubTools();
    const tools = this.toolNames(options.mode);
    const sessionManager = options.persisted
      ? SessionManager.create(options.cwd, this.config.sessionDir)
      : SessionManager.inMemory(options.cwd);
    const services = await createPiServices({
      cwd: options.cwd,
      agentDir: getAgentDir(),
      modelRuntime: this.modelRuntime,
      mode: options.mode,
      remotePiResourcePolicy: this.config.remotePiResourcePolicy,
    });
    const diagnostics = collectPiDiagnostics(services);
    const errors = diagnostics.filter((item) => item.type === "error");
    if (errors.length) {
      throw new Error(errors.map((item) => item.message).join("; "));
    }
    const model = await this.resolveModel();
    const selectedTools = sessionToolSelection(
      options.mode,
      this.config.remoteToolPolicy,
      tools,
    );
    const { session } = await createAgentSessionFromServices({
      services,
      model,
      thinkingLevel: this.config.thinkingLevel as never,
      ...(selectedTools === undefined ? {} : { tools: selectedTools }),
      customTools,
      sessionManager,
      sessionStartEvent: {
        type: "session_start",
        reason: "startup",
      },
    });
    activateCompatibleTools(
      session,
      options.mode,
      this.config.remoteToolPolicy,
    );

    return {
      sessionId: session.sessionId,
      ...(session.sessionFile ? { sessionFile: session.sessionFile } : {}),
      setSessionName: (name) => session.setSessionName(name),
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
      projectTrustContext?: ProjectTrustContext;
    }) => {
      const services = await createPiServices({
        cwd: runtimeOptions.cwd,
        agentDir: runtimeOptions.agentDir,
        modelRuntime: this.modelRuntime,
        mode: "local",
        remotePiResourcePolicy: this.config.remotePiResourcePolicy,
        ...(runtimeOptions.projectTrustContext
          ? { projectTrustContext: runtimeOptions.projectTrustContext }
          : {}),
      });
      const model = await this.resolveModel();
      const created = await createAgentSessionFromServices({
        services,
        sessionManager: runtimeOptions.sessionManager,
        ...(runtimeOptions.sessionStartEvent
          ? { sessionStartEvent: runtimeOptions.sessionStartEvent }
          : {}),
        model,
        thinkingLevel: this.config.thinkingLevel as never,
        customTools: this.createHubTools(),
      });
      activateCompatibleTools(created.session, "local", "full");
      return {
        ...created,
        services,
        diagnostics: collectPiDiagnostics(services),
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

  private async resolveModel(): Promise<
    NonNullable<ReturnType<ModelRuntime["getModel"]>>
  > {
    const model = this.modelRuntime.getModel(this.provider, this.modelId);
    if (!model) {
      throw new Error(`Pi model not found: ${this.provider}/${this.modelId}`);
    }
    assertRemoteUrl(model.baseUrl);
    const auth = await this.modelRuntime.getAuth(model);
    if (!auth) {
      throw new Error(
        `No remote API credential is configured for Pi provider ${this.provider}`,
      );
    }
    return model;
  }

  private toolNames(mode: "local" | "remote" | "diagnostic"): string[] {
    if (mode === "diagnostic") return [];
    const hubTools = this.hub ? HUB_TOOL_NAMES : [];
    if (mode === "local") {
      return [
        "read",
        "bash",
        "edit",
        "write",
        "grep",
        "find",
        "ls",
        ...hubTools,
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
        ...hubTools,
      ];
    }
    if (this.config.remoteToolPolicy === "read_only") {
      return ["read", "grep", "find", "ls", ...hubTools];
    }
    return [...hubTools];
  }

  private createHubTools() {
    if (!this.hub) return [];
    const hub = this.hub;
    const listActors = defineTool({
      name: "hub_list_actors",
      label: "List collaboration actors",
      description:
        "List human, agent, and service actors registered in the coordination Hub.",
      parameters: Type.Object({}),
      execute: async () => this.toolResult(await hub.listActors()),
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
          await hub.listTasks(params.status as TaskStatus | undefined),
        ),
    });
    const getTask = defineTool({
      name: "hub_get_task",
      label: "Get collaboration task",
      description: "Read one task, including its result and artifact references.",
      parameters: Type.Object({ taskId: Type.String({ minLength: 1 }) }),
      execute: async (_id, params) =>
        this.toolResult(await hub.getTask(params.taskId)),
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
          await hub.createTask({
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
