export interface AdapterSessionManifest {
  resume: boolean;
  /** Args used when starting a fresh session. Defaults to top-level args. */
  new_args?: string[];
  /** Args used when resuming a session; must contain {session_id}. */
  resume_args?: string[];
  /** Field name in the result JSON carrying the new session id. Defaults to "session_id". */
  result_field?: string;
  /** Glob (relative to workspace, or ~-prefixed) to discover the newest session file. */
  discovery_glob?: string;
}

export interface AdapterManifest {
  id: string;
  display_name: string;
  capabilities: string[];
  /** Base argv, for example ["opencode", "run"]. */
  command: string[];
  /**
   * Extra args for a one-shot invocation. Supported placeholders:
   * {task_file}, {prompt}, {workspace}, {session_id}.
   */
  args: string[];
  env?: Record<string, string>;
  /** file = read AGENT_RESULT.json next to the task envelope; stdout_json = parse stdout JSON. */
  result_mode: "file" | "stdout_json";
  timeout_seconds?: number;
  cancel_grace_seconds?: number;
  session?: AdapterSessionManifest;
}

export interface AdapterTaskEnvelope {
  task_id: string;
  run_id: string;
  objective: string;
  input: Record<string, unknown>;
  workspace: string;
  capabilities: string[];
  session_id?: string;
  continue: boolean;
}

export interface AdapterArtifact {
  path: string;
  name?: string;
  media_type?: string;
}

export interface AdapterResultFile {
  status?: "completed" | "failed";
  message?: string;
  result?: Record<string, unknown>;
  text?: string;
  session_id?: string;
  sessionID?: string;
  artifacts?: AdapterArtifact[];
}

export interface AdapterSessionScope {
  adapterId: string;
  actorId: string;
  nodeId: string;
  principalId: string;
  workerSlot: number;
  cwd: string;
}

export interface AdapterSessionRecord extends AdapterSessionScope {
  version: 1;
  key: string;
  sessionId: string;
  taskCount: number;
  lastTaskId: string;
  createdAt: string;
  updatedAt: string;
}
