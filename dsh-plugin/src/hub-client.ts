/**
 * Minimal AgentSociety Hub REST client used by the dsh worker plugin.
 * The Hub remains the durable coordination source; this package only speaks
 * the public REST contract.
 */

export type HubTaskStatus =
  | "submitted"
  | "working"
  | "completed"
  | "failed"
  | "cancelled";

export interface HubTask {
  task_id: string;
  principal_id: string;
  delegator_actor_id: string;
  assignee_actor_id: string | null;
  objective: string;
  required_capabilities: string[];
  input: Record<string, unknown>;
  status: HubTaskStatus;
  result: Record<string, unknown>;
  error: string | null;
}

export interface HubRun {
  run_id: string;
  task_id: string | null;
  principal_id: string;
  actor_id: string;
  node_id: string;
  status: "active" | "completed" | "failed" | "cancelled";
  result: Record<string, unknown>;
}

export interface HubTaskControl {
  control_id: string;
  task_id: string;
  run_id: string | null;
  kind: "steer" | "follow_up";
  message: string;
  actor_id: string;
  status: "pending" | "leased" | "delivered" | "unsupported";
  lease_token: string;
}

export interface HubArtifact {
  artifact_id: string;
  task_id: string | null;
  run_id: string | null;
  name: string;
  media_type: string;
  uri: string;
  sha256: string | null;
  size_bytes: number | null;
  created_by_actor_id: string;
  metadata: Record<string, unknown>;
}

export interface HubClaim {
  task: HubTask;
  run: HubRun;
  lease_token: string;
}

export class HubError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "HubError";
  }
}

export class HubClient {
  constructor(
    readonly baseUrl: string,
    readonly token: string,
    private readonly fetchImpl: typeof fetch = fetch,
  ) {}

  async registerPrincipal(item: {
    principal_id: string;
    kind: "human";
    display_name: string;
    metadata?: Record<string, unknown>;
  }): Promise<void> {
    await this.request("/v1/hub/principals", { method: "POST", body: item });
  }

  async registerActor(item: {
    actor_id: string;
    principal_id: string;
    kind: "agent";
    display_name: string;
    capabilities: string[];
    metadata?: Record<string, unknown>;
  }): Promise<void> {
    await this.request("/v1/hub/actors", { method: "POST", body: item });
  }

  async registerNode(item: {
    node_id: string;
    actor_id: string;
    display_name: string;
    capabilities: string[];
    metadata?: Record<string, unknown>;
  }): Promise<void> {
    await this.request("/v1/hub/nodes", { method: "POST", body: item });
  }

  async heartbeat(nodeId: string): Promise<void> {
    await this.request("/v1/hub/nodes/heartbeat", {
      method: "POST",
      body: { node_id: nodeId },
    });
  }

  async claimTask(item: {
    actor_id: string;
    node_id: string;
    wait_seconds: number;
    lease_seconds: number;
  }): Promise<HubClaim | null> {
    const response = await this.request<{ claim: HubClaim | null }>(
      "/v1/hub/tasks/claim",
      { method: "POST", body: item },
    );
    return response.claim;
  }

  async getTask(taskId: string): Promise<HubTask> {
    const response = await this.request<{ task: HubTask }>(
      `/v1/hub/tasks/${encodeURIComponent(taskId)}`,
      { method: "GET" },
    );
    return response.task;
  }

  async claimTaskControls(
    taskId: string,
    item: { run_id: string; lease_token: string },
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

  async updateTask(
    taskId: string,
    item: {
      run_id: string;
      lease_token: string;
      status: Exclude<HubTaskStatus, "submitted">;
      message?: string;
      result?: Record<string, unknown>;
      /** Progressive observer state (phase, tool counts, ...). */
      partial_result?: Record<string, unknown>;
    },
  ): Promise<void> {
    const body: Record<string, unknown> = {
      run_id: item.run_id,
      lease_token: item.lease_token,
      status: item.status,
      message: item.message,
      result: item.result ?? {},
    };
    if (item.partial_result !== undefined) {
      body.partial_result = item.partial_result;
    }
    await this.request(`/v1/hub/tasks/${encodeURIComponent(taskId)}/updates`, {
      method: "POST",
      body,
    });
  }

  /**
   * Append one entry to the principal's shared memory (scope: consensus /
   * directory / qa). Idempotent when `event_id` is supplied: the Hub returns
   * the existing seq for a duplicate.
   */
  async appendSharedEvent(item: {
    scope?: string;
    kind: string;
    payload: Record<string, unknown>;
    principal_id?: string;
    session_id?: string;
    actor_id?: string;
    node_id?: string;
    event_id?: string;
    ttl_hours?: number;
  }): Promise<{ seq: number; event_id: string }> {
    const body: Record<string, unknown> = { ...item };
    const response = await this.request<{ event: { seq: number; event_id: string } }>(
      "/v1/hub/contexts/append",
      { method: "POST", body },
    );
    return response.event;
  }

  /** Incremental pull of the shared memory log (after_seq = resume point). */
  async listSharedEvents(item: {
    after_seq?: number;
    scope?: string;
    kind?: string;
    session_id?: string;
    limit?: number;
  } = {}): Promise<Array<Record<string, unknown>>> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(item)) {
      if (value !== undefined) query.set(key, String(value));
    }
    const response = await this.request<{ events: Array<Record<string, unknown>> }>(
      `/v1/hub/contexts${query.size > 0 ? `?${query.toString()}` : ""}`,
      { method: "GET", body: undefined },
    );
    return response.events;
  }

  /**
   * Subscribe to the worker push channel (`/v1/hub/events`) and invoke
   * `onEvent` for every SSE event. Resolves when the stream ends normally
   * (server closed the connection) and rejects on transport errors; the
   * caller owns reconnection with backoff.
   */
  async subscribeEvents(
    nodeId: string,
    onEvent: (event: { name: string; data: Record<string, unknown> }) => void,
    options: { signal?: AbortSignal } = {},
  ): Promise<void> {
    const url = `${this.baseUrl.replace(/\/$/u, "")}/v1/hub/events?node_id=${encodeURIComponent(nodeId)}`;
    const init: RequestInit = {
      headers: { Authorization: `Bearer ${this.token}` },
    };
    if (options.signal !== undefined) init.signal = options.signal;
    const response = await this.fetchImpl(url, init);
    if (!response.ok) {
      throw new HubError(
        `Hub events subscription failed (${response.status})`,
        response.status,
      );
    }
    if (response.body === null) return;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        let name = "message";
        let data: Record<string, unknown> = {};
        for (const line of block.split("\n")) {
          if (line.startsWith("event: ")) {
            name = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            try {
              const parsed: unknown = JSON.parse(line.slice(6));
              if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
                data = parsed as Record<string, unknown>;
              }
            } catch {
              // Non-JSON data lines are ignored.
            }
          }
        }
        if (name !== "message" || Object.keys(data).length > 0) {
          onEvent({ name, data });
        }
      }
    }
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

  async updateRun(
    runId: string,
    item: { status: "active" | "completed" | "failed" | "cancelled"; result?: Record<string, unknown>; error?: string },
  ): Promise<void> {
    await this.request(`/v1/hub/runs/${encodeURIComponent(runId)}/updates`, {
      method: "POST",
      body: item,
    });
  }

  private async request<T = Record<string, unknown>>(
    path: string,
    init: { method: string; body?: unknown },
  ): Promise<T> {
    const response = await this.fetchImpl(`${this.baseUrl.replace(/\/$/u, "")}${path}`, {
      method: init.method,
      headers: {
        Authorization: `Bearer ${this.token}`,
        ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new HubError(
        `Hub request ${path} failed (${response.status})${detail ? `: ${detail.slice(0, 500)}` : ""}`,
        response.status,
      );
    }
    return (await response.json()) as T;
  }
}
