import { createHash, randomUUID } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { join, resolve } from "node:path";

import type {
  AdapterSessionRecord,
  AdapterSessionScope,
} from "./bridge-types.js";

export class AdapterSessionRegistry {
  private readonly recordsDir: string;

  constructor(private readonly sessionDir: string) {
    this.recordsDir = join(sessionDir, "agent-host-adapter-sessions.d");
  }

  key(scope: AdapterSessionScope): string {
    return createHash("sha256")
      .update(
        JSON.stringify({
          adapterId: scope.adapterId,
          actorId: scope.actorId,
          nodeId: scope.nodeId,
          principalId: scope.principalId,
          workerSlot: scope.workerSlot,
          cwd: resolve(scope.cwd),
        }),
      )
      .digest("hex");
  }

  get(scope: AdapterSessionScope): AdapterSessionRecord | undefined {
    const key = this.key(scope);
    const path = this.recordPath(key);
    if (!existsSync(path)) return undefined;
    try {
      const value = JSON.parse(readFileSync(path, "utf8")) as unknown;
      if (!isAdapterSessionRecord(value)) return undefined;
      if (value.key !== key || !sameScope(value, scope)) return undefined;
      return value;
    } catch {
      return undefined;
    }
  }

  upsert(
    scope: AdapterSessionScope,
    sessionId: string,
    taskId: string,
    options: { reset?: boolean } = {},
  ): AdapterSessionRecord {
    const now = new Date().toISOString();
    const key = this.key(scope);
    const existing = options.reset ? undefined : this.get(scope);
    const sameSession = existing?.sessionId === sessionId;
    const record: AdapterSessionRecord = {
      version: 1,
      key,
      ...scope,
      cwd: resolve(scope.cwd),
      sessionId,
      taskCount: sameSession ? existing.taskCount + 1 : 1,
      lastTaskId: taskId,
      createdAt: sameSession ? existing.createdAt : now,
      updatedAt: now,
    };
    this.write(record);
    return record;
  }

  clear(scope: AdapterSessionScope): void {
    const path = this.recordPath(this.key(scope));
    if (existsSync(path)) {
      rmSync(path, { force: true });
    }
  }

  private recordPath(key: string): string {
    return join(this.recordsDir, `${key}.json`);
  }

  private write(record: AdapterSessionRecord): void {
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
  record: AdapterSessionRecord,
  scope: AdapterSessionScope,
): boolean {
  return (
    record.adapterId === scope.adapterId &&
    record.actorId === scope.actorId &&
    record.nodeId === scope.nodeId &&
    record.principalId === scope.principalId &&
    record.workerSlot === scope.workerSlot &&
    record.cwd === resolve(scope.cwd)
  );
}

function isAdapterSessionRecord(value: unknown): value is AdapterSessionRecord {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    item.version === 1 &&
    typeof item.key === "string" &&
    typeof item.adapterId === "string" &&
    typeof item.actorId === "string" &&
    typeof item.nodeId === "string" &&
    typeof item.principalId === "string" &&
    typeof item.workerSlot === "number" &&
    Number.isInteger(item.workerSlot) &&
    item.workerSlot >= 0 &&
    typeof item.cwd === "string" &&
    typeof item.sessionId === "string" &&
    typeof item.taskCount === "number" &&
    Number.isInteger(item.taskCount) &&
    item.taskCount >= 1 &&
    typeof item.lastTaskId === "string" &&
    typeof item.createdAt === "string" &&
    typeof item.updatedAt === "string"
  );
}
