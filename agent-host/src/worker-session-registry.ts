import { createHash, randomUUID } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { join, relative, resolve } from "node:path";

export interface WorkerSessionScope {
  actorId: string;
  nodeId: string;
  principalId: string;
  workerSlot: number;
  cwd: string;
}

export interface WorkerSessionRecord extends WorkerSessionScope {
  version: 1;
  key: string;
  sessionId: string;
  sessionFile: string;
  taskCount: number;
  lastTaskId: string;
  createdAt: string;
  updatedAt: string;
}

export class WorkerSessionRegistry {
  private readonly recordsDir: string;

  constructor(private readonly sessionDir: string) {
    this.recordsDir = join(sessionDir, "agent-host-worker-sessions.d");
  }

  key(scope: WorkerSessionScope): string {
    return createHash("sha256")
      .update(
        JSON.stringify({
          actorId: scope.actorId,
          nodeId: scope.nodeId,
          principalId: scope.principalId,
          workerSlot: scope.workerSlot,
          cwd: resolve(scope.cwd),
        }),
      )
      .digest("hex");
  }

  get(scope: WorkerSessionScope): WorkerSessionRecord | undefined {
    const key = this.key(scope);
    const path = this.recordPath(key);
    if (!existsSync(path)) return undefined;
    try {
      const value = JSON.parse(readFileSync(path, "utf8")) as unknown;
      if (!isWorkerSessionRecord(value)) return undefined;
      if (value.key !== key || !sameScope(value, scope)) return undefined;
      if (!isSessionFileInside(this.sessionDir, value.sessionFile)) return undefined;
      if (!existsSync(value.sessionFile)) return undefined;
      return value;
    } catch {
      return undefined;
    }
  }

  upsert(
    scope: WorkerSessionScope,
    session: { sessionId: string; sessionFile: string },
    taskId: string,
    options: { reset?: boolean } = {},
  ): WorkerSessionRecord {
    if (!isSessionFileInside(this.sessionDir, session.sessionFile)) {
      throw new Error("Worker session file must be inside AGENT_SESSION_DIR");
    }
    const now = new Date().toISOString();
    const key = this.key(scope);
    const existing = options.reset ? undefined : this.get(scope);
    const sameSession = existing?.sessionId === session.sessionId;
    const record: WorkerSessionRecord = {
      version: 1,
      key,
      ...scope,
      cwd: resolve(scope.cwd),
      sessionId: session.sessionId,
      sessionFile: resolve(session.sessionFile),
      taskCount: sameSession ? existing.taskCount + 1 : 1,
      lastTaskId: taskId,
      createdAt: sameSession ? existing.createdAt : now,
      updatedAt: now,
    };
    this.write(record);
    return record;
  }

  private recordPath(key: string): string {
    return join(this.recordsDir, `${key}.json`);
  }

  private write(record: WorkerSessionRecord): void {
    mkdirSync(this.recordsDir, { recursive: true, mode: 0o700 });
    chmodSync(this.recordsDir, 0o700);
    const target = this.recordPath(record.key);
    const temporary = `${target}.${process.pid}.${randomUUID()}.tmp`;
    writeFileSync(temporary, `${JSON.stringify(record, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    chmodSync(temporary, 0o600);
    renameSync(temporary, target);
  }
}

function sameScope(
  record: WorkerSessionRecord,
  scope: WorkerSessionScope,
): boolean {
  return (
    record.actorId === scope.actorId &&
    record.nodeId === scope.nodeId &&
    record.principalId === scope.principalId &&
    record.workerSlot === scope.workerSlot &&
    record.cwd === resolve(scope.cwd)
  );
}

function isSessionFileInside(sessionDir: string, sessionFile: string): boolean {
  const pathFromRoot = relative(resolve(sessionDir), resolve(sessionFile));
  return (
    pathFromRoot !== ".." &&
    !pathFromRoot.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`) &&
    pathFromRoot !== ""
  );
}

function isWorkerSessionRecord(value: unknown): value is WorkerSessionRecord {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    item.version === 1 &&
    typeof item.key === "string" &&
    typeof item.actorId === "string" &&
    typeof item.nodeId === "string" &&
    typeof item.principalId === "string" &&
    typeof item.workerSlot === "number" &&
    Number.isInteger(item.workerSlot) &&
    item.workerSlot >= 0 &&
    typeof item.cwd === "string" &&
    typeof item.sessionId === "string" &&
    typeof item.sessionFile === "string" &&
    typeof item.taskCount === "number" &&
    Number.isInteger(item.taskCount) &&
    item.taskCount >= 1 &&
    typeof item.lastTaskId === "string" &&
    typeof item.createdAt === "string" &&
    typeof item.updatedAt === "string"
  );
}
