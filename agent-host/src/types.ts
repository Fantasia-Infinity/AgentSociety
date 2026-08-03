export interface Principal {
  principal_id: string;
  kind: "human" | "agent" | "service" | "organization";
  display_name: string;
  metadata: Record<string, unknown>;
}

export interface Actor {
  actor_id: string;
  principal_id: string;
  kind: "human" | "agent" | "service";
  display_name: string;
  capabilities: string[];
  metadata: Record<string, unknown>;
}

export interface NodeRecord {
  node_id: string;
  actor_id: string;
  display_name: string;
  capabilities: string[];
  metadata: Record<string, unknown>;
  status: string;
}

export type TaskStatus =
  | "submitted"
  | "working"
  | "completed"
  | "failed"
  | "cancelled";

export interface HubTask {
  task_id: string;
  context_id: string | null;
  principal_id: string;
  delegator_actor_id: string;
  assignee_actor_id: string | null;
  executor_actor_id?: string | null;
  executor_node_id?: string | null;
  objective: string;
  required_capabilities: string[];
  input: Record<string, unknown>;
  metadata: Record<string, unknown>;
  origin: string;
  status: TaskStatus;
  result: Record<string, unknown>;
  error: string | null;
  lease_until?: number;
  lease_seconds?: number;
}

export interface HubTaskControl {
  control_id: string;
  task_id: string;
  run_id: string | null;
  kind: "steer" | "follow_up";
  message: string;
  actor_id: string;
  status: "pending" | "leased" | "delivered";
  lease_token: string;
}

export interface HubRun {
  run_id: string;
  task_id: string | null;
  principal_id: string;
  actor_id: string;
  node_id: string;
  origin: string;
  objective: string | null;
  status: "active" | "completed" | "failed" | "cancelled";
  result: Record<string, unknown>;
  error: string | null;
}

export interface HubTaskEvent {
  seq: number;
  event_id: string;
  task_id: string;
  run_id: string | null;
  type: string;
  actor_id: string | null;
  node_id: string | null;
  message: string | null;
  payload: Record<string, unknown>;
  created_at: number;
}

export interface HubClaim {
  task: HubTask;
  run: HubRun;
  lease_token: string;
}

export interface AgentResult {
  text: string;
  provider: string;
  model: string;
  sessionId: string;
}

export interface AgentSessionPosition {
  entryCount: number;
  messageCount: number;
}

export interface AgentTaskContext {
  taskId: string;
  runId: string;
}

export interface AgentConversation {
  readonly sessionId: string;
  readonly sessionFile?: string;
  prompt(text: string, onText?: (delta: string) => void): Promise<AgentResult>;
  steer?(text: string): Promise<void>;
  followUp?(text: string): Promise<void>;
  abort?(): Promise<void>;
  getSessionPosition?(): AgentSessionPosition;
  setTaskContext?(context?: AgentTaskContext): void;
  setSessionName(name: string): void;
  dispose(): Promise<void>;
}

export interface AgentEngine {
  createConversation(options: {
    cwd: string;
    mode: "local" | "remote" | "diagnostic";
    persisted: boolean;
    sessionFile?: string;
    subagentDepth?: number;
  }): Promise<AgentConversation>;
}
