import type {
  Actor,
  HubArtifact,
  HubClaim,
  HubRun,
  HubTask,
  HubTaskControl,
  HubTaskEvent,
  NodeRecord,
  Principal,
  TaskStatus,
} from "./types.js";

type FetchLike = typeof fetch;

export class HubError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export class HubClient {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string,
    private readonly fetchImpl: FetchLike = fetch,
  ) {}

  /** The bearer token used by this client. Exposed only for trusted child runtimes. */
  get nodeToken(): string {
    return this.token;
  }

  async registerPrincipal(item: {
    principal_id: string;
    kind: Principal["kind"];
    display_name: string;
    metadata?: Record<string, unknown>;
  }): Promise<Principal> {
    const response = await this.request<{ principal: Principal }>(
      "/v1/hub/principals",
      { method: "POST", body: item },
    );
    return response.principal;
  }

  async registerActor(item: {
    actor_id: string;
    principal_id: string;
    kind: Actor["kind"];
    display_name: string;
    capabilities: string[];
    metadata?: Record<string, unknown>;
  }): Promise<Actor> {
    const response = await this.request<{ actor: Actor }>("/v1/hub/actors", {
      method: "POST",
      body: item,
    });
    return response.actor;
  }

  async registerNode(item: {
    node_id: string;
    actor_id: string;
    display_name: string;
    capabilities: string[];
    metadata?: Record<string, unknown>;
  }): Promise<NodeRecord> {
    const response = await this.request<{ node: NodeRecord }>("/v1/hub/nodes", {
      method: "POST",
      body: item,
    });
    return response.node;
  }

  async heartbeat(nodeId: string): Promise<void> {
    await this.request("/v1/hub/nodes/heartbeat", {
      method: "POST",
      body: { node_id: nodeId },
    });
  }

  async listActors(): Promise<Actor[]> {
    const response = await this.request<{ actors: Actor[] }>("/v1/hub/actors");
    return response.actors;
  }

  async listTasks(status?: TaskStatus): Promise<HubTask[]> {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    const response = await this.request<{ tasks: HubTask[] }>(
      `/v1/hub/tasks${query}`,
    );
    return response.tasks;
  }

  async getTask(taskId: string): Promise<HubTask> {
    const response = await this.request<{ task: HubTask }>(
      `/v1/hub/tasks/${encodeURIComponent(taskId)}`,
    );
    return response.task;
  }

  async getTaskEvents(taskId: string): Promise<HubTaskEvent[]> {
    const response = await this.request<{ events: HubTaskEvent[] }>(
      `/v1/hub/tasks/${encodeURIComponent(taskId)}/events`,
    );
    return response.events;
  }

  async getRun(runId: string): Promise<HubRun> {
    const response = await this.request<{ run: HubRun }>(
      `/v1/hub/runs/${encodeURIComponent(runId)}`,
    );
    return response.run;
  }

  async createTask(item: {
    principal_id: string;
    delegator_actor_id: string;
    objective: string;
    assignee_actor_id?: string;
    required_capabilities?: string[];
    input?: Record<string, unknown>;
    metadata?: Record<string, unknown>;
    origin?: string;
    context_id?: string;
    idempotency_key?: string;
  }): Promise<HubTask> {
    const response = await this.request<{ task: HubTask }>("/v1/hub/tasks", {
      method: "POST",
      body: item,
    });
    return response.task;
  }

  async claimTask(item: {
    actor_id: string;
    node_id: string;
    wait_seconds: number;
    lease_seconds: number;
  }, signal?: AbortSignal): Promise<HubClaim | null> {
    const response = await this.request<{ claim: HubClaim | null }>(
      "/v1/hub/tasks/claim",
      {
        method: "POST",
        body: item,
        timeoutMs: (item.wait_seconds + 10) * 1000,
        ...(signal ? { signal } : {}),
      },
    );
    return response.claim;
  }

  async updateTask(
    taskId: string,
    item: {
      run_id: string;
      lease_token: string;
      status: Exclude<TaskStatus, "submitted">;
      message?: string;
      result?: Record<string, unknown>;
    },
  ): Promise<HubTask> {
    const response = await this.request<{ task: HubTask }>(
      `/v1/hub/tasks/${encodeURIComponent(taskId)}/updates`,
      { method: "POST", body: item },
    );
    return response.task;
  }

  async createTaskControl(
    taskId: string,
    item: { actor_id: string; kind: "steer" | "follow_up"; message: string },
  ): Promise<HubTaskControl> {
    const response = await this.request<{ control: HubTaskControl }>(
      `/v1/hub/tasks/${encodeURIComponent(taskId)}/controls`,
      { method: "POST", body: item },
    );
    return response.control;
  }

  async cancelTask(
    taskId: string,
    item: { actor_id: string; reason?: string },
  ): Promise<HubTask> {
    const response = await this.request<{ task: HubTask }>(
      `/v1/hub/tasks/${encodeURIComponent(taskId)}/cancel`,
      { method: "POST", body: item },
    );
    return response.task;
  }

  async claimTaskControls(
    taskId: string,
    item: { run_id: string; lease_token: string; limit?: number },
  ): Promise<HubTaskControl[]> {
    const response = await this.request<{ controls: HubTaskControl[] }>(
      `/v1/hub/tasks/${encodeURIComponent(taskId)}/controls/claim`,
      { method: "POST", body: item },
    );
    return response.controls;
  }

  async acknowledgeTaskControl(
    taskId: string,
    controlId: string,
    item: { run_id: string; lease_token: string },
  ): Promise<void> {
    await this.request(
      `/v1/hub/tasks/${encodeURIComponent(taskId)}/controls/${encodeURIComponent(controlId)}/ack`,
      { method: "POST", body: item },
    );
  }

  async markTaskControlUnsupported(
    taskId: string,
    controlId: string,
    item: { run_id: string; lease_token: string; reason: string },
  ): Promise<void> {
    await this.request(
      `/v1/hub/tasks/${encodeURIComponent(taskId)}/controls/${encodeURIComponent(controlId)}/unsupported`,
      { method: "POST", body: item },
    );
  }

  async startRun(item: {
    principal_id: string;
    actor_id: string;
    node_id: string;
    origin: string;
    objective?: string;
    task_id?: string;
    metadata?: Record<string, unknown>;
  }): Promise<HubRun> {
    const response = await this.request<{ run: HubRun }>("/v1/hub/runs", {
      method: "POST",
      body: item,
    });
    return response.run;
  }

  async updateRun(
    runId: string,
    item: {
      status: "active" | "completed" | "failed" | "cancelled";
      result?: Record<string, unknown>;
      error?: string;
    },
  ): Promise<HubRun> {
    const response = await this.request<{ run: HubRun }>(
      `/v1/hub/runs/${encodeURIComponent(runId)}/updates`,
      { method: "POST", body: item },
    );
    return response.run;
  }

  async addArtifact(item: {
    name: string;
    media_type: string;
    task_id?: string;
    run_id?: string;
    created_by_actor_id: string;
    content_base64?: string;
    uri?: string;
    sha256?: string;
    size_bytes?: number;
    metadata?: Record<string, unknown>;
  }): Promise<HubArtifact> {
    const response = await this.request<{ artifact: HubArtifact }>(
      "/v1/hub/artifacts",
      { method: "POST", body: item },
    );
    return response.artifact;
  }

  async agentLogin(item: {
    username: string;
    password: string;
    node_id: string;
    actor_id?: string;
    display_name?: string;
    capabilities?: string[];
    metadata?: Record<string, unknown>;
  }): Promise<{
    node_token: string;
    node: NodeRecord;
    actor: Actor;
    principal: Principal;
    tenant_id: string;
    user: {
      username: string;
      display_name: string;
      principal_id: string;
      tenant_id: string;
      role: string;
    };
  }> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30_000);
    try {
      const response = await this.fetchImpl(`${this.baseUrl}/v1/auth/agent-login`, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(item),
        signal: controller.signal,
      });
      const payload = (await response.json()) as Record<string, unknown>;
      if (!response.ok) {
        const message =
          typeof payload.error === "string"
            ? payload.error
            : "Hub agent-login failed";
        throw new HubError(message, response.status);
      }
      return payload as unknown as Awaited<ReturnType<HubClient["agentLogin"]>>;
    } finally {
      clearTimeout(timeout);
    }
  }

  private async request<T = Record<string, unknown>>(
    path: string,
    options: {
      method?: "GET" | "POST";
      body?: object;
      timeoutMs?: number;
      signal?: AbortSignal;
    } = {},
  ): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      options.timeoutMs ?? 30_000,
    );
    const abort = () => controller.abort();
    options.signal?.addEventListener("abort", abort, { once: true });
    if (options.signal?.aborted) controller.abort();
    try {
      const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: options.method ?? "GET",
        headers: {
          Authorization: `Bearer ${this.token}`,
          Accept: "application/json",
          ...(options.body ? { "Content-Type": "application/json" } : {}),
        },
        ...(options.body ? { body: JSON.stringify(options.body) } : {}),
        signal: controller.signal,
      });
      const payload = (await response.json()) as Record<string, unknown>;
      if (!response.ok) {
        const message =
          typeof payload.error === "string" ? payload.error : "Hub request failed";
        throw new HubError(message, response.status);
      }
      return payload as T;
    } finally {
      clearTimeout(timeout);
      options.signal?.removeEventListener("abort", abort);
    }
  }
}
