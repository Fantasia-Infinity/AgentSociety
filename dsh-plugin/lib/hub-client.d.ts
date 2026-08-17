/**
 * Minimal AgentSociety Hub REST client used by the dsh worker plugin.
 * The Hub remains the durable coordination source; this package only speaks
 * the public REST contract.
 */
export type HubTaskStatus = "submitted" | "working" | "completed" | "failed" | "cancelled";
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
export declare class HubError extends Error {
    readonly status: number;
    constructor(message: string, status: number);
}
export declare class HubClient {
    readonly baseUrl: string;
    readonly token: string;
    private readonly fetchImpl;
    constructor(baseUrl: string, token: string, fetchImpl?: typeof fetch);
    registerPrincipal(item: {
        principal_id: string;
        kind: "human";
        display_name: string;
        metadata?: Record<string, unknown>;
    }): Promise<void>;
    registerActor(item: {
        actor_id: string;
        principal_id: string;
        kind: "agent";
        display_name: string;
        capabilities: string[];
        metadata?: Record<string, unknown>;
    }): Promise<void>;
    registerNode(item: {
        node_id: string;
        actor_id: string;
        display_name: string;
        capabilities: string[];
        metadata?: Record<string, unknown>;
    }): Promise<void>;
    heartbeat(nodeId: string): Promise<void>;
    claimTask(item: {
        actor_id: string;
        node_id: string;
        wait_seconds: number;
        lease_seconds: number;
    }): Promise<HubClaim | null>;
    getTask(taskId: string): Promise<HubTask>;
    claimTaskControls(taskId: string, item: {
        run_id: string;
        lease_token: string;
    }): Promise<HubTaskControl[]>;
    acknowledgeTaskControl(taskId: string, controlId: string, item: {
        run_id: string;
        lease_token: string;
    }): Promise<void>;
    updateTask(taskId: string, item: {
        run_id: string;
        lease_token: string;
        status: Exclude<HubTaskStatus, "submitted">;
        message?: string;
        result?: Record<string, unknown>;
        /** Progressive observer state (phase, tool counts, ...). */
        partial_result?: Record<string, unknown>;
    }): Promise<void>;
    /**
     * Append one entry to the principal's shared memory (scope: consensus /
     * directory / qa). Idempotent when `event_id` is supplied: the Hub returns
     * the existing seq for a duplicate.
     */
    appendSharedEvent(item: {
        scope?: string;
        kind: string;
        payload: Record<string, unknown>;
        principal_id?: string;
        session_id?: string;
        actor_id?: string;
        node_id?: string;
        event_id?: string;
        ttl_hours?: number;
    }): Promise<{
        seq: number;
        event_id: string;
    }>;
    /** Incremental pull of the shared memory log (after_seq = resume point). */
    listSharedEvents(item?: {
        after_seq?: number;
        scope?: string;
        kind?: string;
        session_id?: string;
        limit?: number;
    }): Promise<Array<Record<string, unknown>>>;
    /**
     * Subscribe to the worker push channel (`/v1/hub/events`) and invoke
     * `onEvent` for every SSE event. Resolves when the stream ends normally
     * (server closed the connection) and rejects on transport errors; the
     * caller owns reconnection with backoff.
     */
    subscribeEvents(nodeId: string, onEvent: (event: {
        name: string;
        data: Record<string, unknown>;
    }) => void, options?: {
        signal?: AbortSignal;
    }): Promise<void>;
    addArtifact(item: {
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
    }): Promise<HubArtifact>;
    updateRun(runId: string, item: {
        status: "active" | "completed" | "failed" | "cancelled";
        result?: Record<string, unknown>;
        error?: string;
    }): Promise<void>;
    private request;
}
