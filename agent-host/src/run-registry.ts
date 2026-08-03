import {
  chmodSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";

export type LocalRunStatus = "active" | "completed" | "failed" | "cancelled";

export interface RunSessionRecord {
  runId: string;
  taskId?: string;
  sessionId: string;
  sessionFile: string;
  cwd: string;
  origin: "local_ui" | "remote_task";
  sessionMode?: "per_task" | "continuous";
  workerSlot?: number;
  workerSessionKey?: string;
  sessionReused?: boolean;
  turnStartEntry?: number;
  turnEndEntry?: number;
  status: LocalRunStatus;
  startedAt: string;
  updatedAt: string;
}

export class RunSessionRegistry {
  private readonly path: string;
  private readonly recordsDir: string;

  constructor(sessionDir: string) {
    this.path = join(sessionDir, "agent-host-runs.json");
    this.recordsDir = join(sessionDir, "agent-host-runs.d");
  }

  upsert(
    item: Omit<RunSessionRecord, "startedAt" | "updatedAt"> & {
      startedAt?: string;
    },
  ): RunSessionRecord {
    const now = new Date().toISOString();
    const existing = this.get(item.runId);
    const record: RunSessionRecord = {
      ...item,
      startedAt: item.startedAt ?? existing?.startedAt ?? now,
      updatedAt: now,
    };
    this.writeOne(record);
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
    const records = new Map<string, RunSessionRecord>();
    if (existsSync(this.path)) {
      const value = JSON.parse(readFileSync(this.path, "utf8")) as unknown;
      if (!Array.isArray(value)) throw new Error("Agent run registry is corrupted");
      for (const record of value.filter(isRunSessionRecord)) {
        records.set(record.runId, record);
      }
    }
    if (existsSync(this.recordsDir)) {
      for (const name of readdirSync(this.recordsDir)) {
        if (!name.endsWith(".json")) continue;
        try {
          const value = JSON.parse(
            readFileSync(join(this.recordsDir, name), "utf8"),
          ) as unknown;
          if (!isRunSessionRecord(value)) continue;
          const existing = records.get(value.runId);
          if (!existing || value.updatedAt >= existing.updatedAt) {
            records.set(value.runId, value);
          }
        } catch {
          // A single interrupted or externally edited record must not hide
          // every other observable session.
        }
      }
    }
    return [...records.values()];
  }

  private writeOne(record: RunSessionRecord): void {
    mkdirSync(this.recordsDir, { recursive: true, mode: 0o700 });
    const target = join(
      this.recordsDir,
      `${encodeURIComponent(record.runId)}.json`,
    );
    const temporary = `${target}.${process.pid}.tmp`;
    writeFileSync(temporary, `${JSON.stringify(record, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    chmodSync(temporary, 0o600);
    renameSync(temporary, target);
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
