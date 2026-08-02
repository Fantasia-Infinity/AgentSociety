import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";

export type LocalRunStatus = "active" | "completed" | "failed" | "cancelled";

export interface RunSessionRecord {
  runId: string;
  taskId?: string;
  sessionId: string;
  sessionFile: string;
  cwd: string;
  origin: "local_ui" | "remote_task";
  status: LocalRunStatus;
  startedAt: string;
  updatedAt: string;
}

export class RunSessionRegistry {
  private readonly path: string;

  constructor(sessionDir: string) {
    this.path = join(sessionDir, "agent-host-runs.json");
  }

  upsert(
    item: Omit<RunSessionRecord, "startedAt" | "updatedAt"> & {
      startedAt?: string;
    },
  ): RunSessionRecord {
    const records = this.readAll();
    const now = new Date().toISOString();
    const existing = records.find((record) => record.runId === item.runId);
    const record: RunSessionRecord = {
      ...item,
      startedAt: item.startedAt ?? existing?.startedAt ?? now,
      updatedAt: now,
    };
    const next = records.filter((entry) => entry.runId !== item.runId);
    next.push(record);
    this.writeAll(next);
    return record;
  }

  updateStatus(runId: string, status: LocalRunStatus): RunSessionRecord | undefined {
    const record = this.get(runId);
    if (!record) return undefined;
    return this.upsert({ ...record, status, startedAt: record.startedAt });
  }

  get(id: string): RunSessionRecord | undefined {
    return this.readAll().find(
      (record) =>
        record.runId === id || record.taskId === id || record.sessionId === id,
    );
  }

  list(): RunSessionRecord[] {
    return this.readAll().sort((left, right) =>
      right.updatedAt.localeCompare(left.updatedAt),
    );
  }

  private readAll(): RunSessionRecord[] {
    if (!existsSync(this.path)) return [];
    const value = JSON.parse(readFileSync(this.path, "utf8")) as unknown;
    if (!Array.isArray(value)) throw new Error("Agent run registry is corrupted");
    return value.filter(isRunSessionRecord);
  }

  private writeAll(records: RunSessionRecord[]): void {
    mkdirSync(dirname(this.path), { recursive: true, mode: 0o700 });
    const temporary = `${this.path}.${process.pid}.tmp`;
    writeFileSync(temporary, `${JSON.stringify(records, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    chmodSync(temporary, 0o600);
    renameSync(temporary, this.path);
  }
}

function isRunSessionRecord(value: unknown): value is RunSessionRecord {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.runId === "string" &&
    typeof item.sessionId === "string" &&
    typeof item.sessionFile === "string" &&
    typeof item.cwd === "string" &&
    typeof item.origin === "string" &&
    typeof item.status === "string" &&
    typeof item.startedAt === "string" &&
    typeof item.updatedAt === "string"
  );
}
