import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { stdin, stdout } from "node:process";

import type { AgentHostConfig } from "./config.js";
import type { HubClient } from "./hub-client.js";
import { RunSessionRegistry, type RunSessionRecord } from "./run-registry.js";
import type { HubRun, HubTask } from "./types.js";

interface Snapshot {
  local?: RunSessionRecord;
  run?: HubRun;
  task?: HubTask;
  transcript: string[];
  error?: string;
}

export async function observeRun(
  config: AgentHostConfig,
  hub: HubClient,
  id: string,
): Promise<void> {
  const registry = new RunSessionRegistry(config.sessionDir);
  let stopped = false;
  const onInput = (data: Buffer) => {
    if (data.includes(3) || data.toString("utf8").toLowerCase().includes("q")) {
      stopped = true;
    }
  };
  const interactive = Boolean(stdin.isTTY && stdout.isTTY);
  if (interactive) {
    stdin.setRawMode?.(true);
    stdin.resume();
    stdin.on("data", onInput);
    stdout.write("\x1b[?1049h\x1b[?25l");
  }
  try {
    do {
      const snapshot = await loadSnapshot(registry, hub, id);
      renderSnapshot(snapshot, interactive);
      if (!interactive) return;
      if (isTerminal(snapshot)) return;
      await delay(1_000);
    } while (!stopped);
  } finally {
    if (interactive) {
      stdin.off("data", onInput);
      stdin.setRawMode?.(false);
      stdin.pause();
      stdout.write("\x1b[?25h\x1b[?1049l");
    }
  }
}

async function loadSnapshot(
  registry: RunSessionRegistry,
  hub: HubClient,
  id: string,
): Promise<Snapshot> {
  const local = registry.get(id);
  let run: HubRun | undefined;
  let task: HubTask | undefined;
  let error: string | undefined;
  try {
    if (id.startsWith("run_")) {
      run = await hub.getRun(id);
      if (run.task_id) task = await hub.getTask(run.task_id);
    } else {
      task = await hub.getTask(id);
      const events = await hub.getTaskEvents(id);
      const runId = [...events]
        .reverse()
        .find((event) => typeof event.run_id === "string")?.run_id;
      if (runId) run = await hub.getRun(runId);
    }
  } catch (reason) {
    error = reason instanceof Error ? reason.message : String(reason);
  }
  const resolvedLocal = local ?? (run ? registry.get(run.run_id) : undefined);
  return {
    ...(resolvedLocal ? { local: resolvedLocal } : {}),
    ...(run ? { run } : {}),
    ...(task ? { task } : {}),
    transcript: resolvedLocal
      ? readTranscript(resolvedLocal, 18)
      : [],
    ...(error ? { error } : {}),
  };
}

function renderSnapshot(snapshot: Snapshot, clear: boolean): void {
  const width = Math.max(Math.min(stdout.columns || 100, 140), 60);
  const line = "─".repeat(width);
  const output = [
    ...(clear ? ["\x1b[H\x1b[2J"] : []),
    "Agent Session Observer",
    line,
    `Run:      ${snapshot.run?.run_id ?? snapshot.local?.runId ?? "unknown"}`,
    `Task:     ${snapshot.task?.task_id ?? snapshot.local?.taskId ?? "local"}`,
    `Status:   ${snapshot.run?.status ?? snapshot.local?.status ?? "unknown"}`,
    `Actor:    ${snapshot.run?.actor_id ?? "unknown"}`,
    `Node:     ${snapshot.run?.node_id ?? "unknown"}`,
    `Session:  ${snapshot.local?.sessionId ?? sessionId(snapshot.run) ?? "remote/not published"}`,
    `Workspace:${snapshot.local ? ` ${snapshot.local.cwd}` : " unavailable on this device"}`,
    line,
    `Objective: ${snapshot.task?.objective ?? snapshot.run?.objective ?? ""}`,
    line,
    ...(snapshot.transcript.length
      ? snapshot.transcript
      : ["No local transcript is available on this device."]),
    ...(snapshot.error ? [line, `Hub error: ${snapshot.error}`] : []),
    line,
    clear ? "Press q or Ctrl-C to leave. Active sessions refresh every second." : "",
  ];
  stdout.write(`${output.join("\n")}\n`);
}

export function readTranscript(
  record: RunSessionRecord,
  limit: number,
): string[] {
  const marker = readSessionMarker(record.sessionFile);
  const engine = record.engine ?? marker?.engine;
  if (engine === "dsh") {
    const transcriptFile = record.transcriptFile ?? marker?.transcriptFile;
    return transcriptFile
      ? readDshTranscript(transcriptFile, limit)
      : ["DeepSeek Harness transcript path is unavailable for this run."];
  }
  return readPiTranscript(record.sessionFile, limit);
}

function readPiTranscript(path: string, limit: number): string[] {
  if (!existsSync(path)) return ["Session file has not been created yet."];
  const messages: string[] = [];
  for (const line of readFileSync(path, "utf8").split(/\r?\n/u)) {
    if (!line.trim()) continue;
    try {
      const entry = JSON.parse(line) as Record<string, unknown>;
      if (entry.type !== "message" || !isRecord(entry.message)) continue;
      const role = String(entry.message.role ?? "message");
      const text = messageText(entry.message.content);
      if (text) messages.push(`${role}> ${text.replace(/\s+/gu, " ").trim()}`);
    } catch {
      // The worker may be appending the final JSONL line while it is read.
    }
  }
  return messages.slice(-limit);
}

function readSessionMarker(
  path: string,
): { engine?: "pi" | "dsh"; transcriptFile?: string } | undefined {
  if (!existsSync(path)) return undefined;
  try {
    const value = JSON.parse(readFileSync(path, "utf8")) as unknown;
    if (!isRecord(value)) return undefined;
    const engine =
      value.engine === "pi" || value.engine === "dsh" ? value.engine : undefined;
    const transcriptFile =
      typeof value.transcriptFile === "string" ? value.transcriptFile : undefined;
    return {
      ...(engine ? { engine } : {}),
      ...(transcriptFile ? { transcriptFile } : {}),
    };
  } catch {
    return undefined;
  }
}

function readDshTranscript(path: string, limit: number): string[] {
  if (!existsSync(path)) {
    return ["DeepSeek Harness session transcript has not been created yet."];
  }
  let text: string;
  if (path.endsWith(".jsonl.zstd")) {
    try {
      text = execFileSync("zstd", ["-dc", path], {
        encoding: "utf8",
        maxBuffer: 8 * 1024 * 1024,
      });
    } catch {
      return [
        "DeepSeek Harness transcript is zstd-compressed and the `zstd` command is unavailable on this device. Set AGENT_DSH_SESSION_COMPRESSION=none for readable transcripts.",
      ];
    }
  } else {
    text = readFileSync(path, "utf8");
  }
  const messages: string[] = [];
  for (const line of text.split(/\r?\n/u)) {
    if (!line.trim()) continue;
    try {
      const entry = JSON.parse(line) as Record<string, unknown>;
      const data = isRecord(entry.data) ? entry.data : undefined;
      if (entry.type === "user/message") {
        const value = data ? messageText(data.content) : "";
        if (value) messages.push(`user> ${value.replace(/\s+/gu, " ").trim()}`);
      } else if (entry.type === "assistant/message" && isRecord(data?.message)) {
        const value = messageText(data.message.content);
        if (value) messages.push(`assistant> ${value.replace(/\s+/gu, " ").trim()}`);
      } else if (entry.type === "tool/call") {
        const name = isRecord(data) && typeof data.name === "string" ? data.name : "tool";
        messages.push(`tool> ${name}`);
      } else if (entry.type === "tool/result") {
        messages.push("tool> result");
      }
    } catch {
      // The runtime may be appending the final JSONL line while it is read.
    }
  }
  return messages.slice(-limit);
}

function messageText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter(isRecord)
    .map((item) => {
      if (item.type === "text" || item.type === "thinking") {
        return String(item.text ?? item.content ?? "");
      }
      if (item.type === "toolCall") return `[tool ${String(item.name ?? "unknown")}]`;
      return "";
    })
    .filter(Boolean)
    .join(" ");
}

function sessionId(run: HubRun | undefined): string | undefined {
  const value = run?.result.pi_session_id ?? run?.result.dsh_session_id;
  return typeof value === "string" ? value : undefined;
}

function isTerminal(snapshot: Snapshot): boolean {
  const status = snapshot.run?.status ?? snapshot.local?.status;
  return status === "completed" || status === "failed" || status === "cancelled";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function delay(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}
