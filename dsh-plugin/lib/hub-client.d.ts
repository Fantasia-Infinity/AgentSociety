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
    updateTask(taskId: string, item: {
        run_id: string;
        lease_token: string;
        status: Exclude<HubTaskStatus, "submitted">;
        message?: string;
        result?: Record<string, unknown>;
    }): Promise<void>;
    updateRun(runId: string, item: {
        status: "active" | "completed" | "failed" | "cancelled";
        result?: Record<string, unknown>;
        error?: string;
    }): Promise<void>;
    private request;
}
