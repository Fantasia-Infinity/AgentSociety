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
    },
  ): Promise<void> {
    await this.request(`/v1/hub/tasks/${encodeURIComponent(taskId)}/updates`, {
      method: "POST",
      body: item,
    });
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
