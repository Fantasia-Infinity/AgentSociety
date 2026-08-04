import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { AdapterManifest, AdapterSessionManifest } from "./bridge-types.js";

const KNOWN_PLACEHOLDERS = new Set([
  "task_file",
  "prompt",
  "workspace",
  "session_id",
  "sandbox",
]);

export function builtinAdaptersDir(): string {
  return resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "adapters");
}

export function listAdapterIds(extraDir?: string): string[] {
  const directories = [
    ...(extraDir ? [resolve(extraDir)] : []),
    builtinAdaptersDir(),
  ];
  const ids = new Set<string>();
  for (const directory of directories) {
    if (!existsSync(directory)) continue;
    for (const entry of readdirSync(directory)) {
      if (extname(entry) !== ".json") continue;
      const id = entry.slice(0, -".json".length);
      if (/^[a-z0-9][a-z0-9_-]*$/u.test(id)) ids.add(id);
    }
  }
  return [...ids].sort();
}

export function loadAdapterManifest(
  id: string,
  extraDir?: string,
): AdapterManifest {
  if (!/^[a-z0-9][a-z0-9_-]*$/u.test(id)) {
    throw new Error(`Invalid adapter id: ${id}`);
  }
  const candidates: string[] = [];
  if (extraDir) candidates.push(join(resolve(extraDir), `${id}.json`));
  candidates.push(join(builtinAdaptersDir(), `${id}.json`));
  const path = candidates.find((candidate) => existsSync(candidate));
  if (!path) {
    throw new Error(
      `Adapter '${id}' not found. Available: ${listAdapterIds(extraDir).join(", ")}`,
    );
  }
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`Adapter '${id}' is not valid JSON: ${message(error)}`);
  }
  return validateAdapterManifest(raw);
}

export function validateAdapterManifest(value: unknown): AdapterManifest {
  if (typeof value !== "object" || value === null) {
    throw new Error("Adapter manifest must be a JSON object");
  }
  const item = value as Record<string, unknown>;
  const id = requiredString(item, "id");
  if (!/^[a-z0-9][a-z0-9_-]*$/u.test(id)) {
    throw new Error("Adapter id must match [a-z0-9][a-z0-9_-]*");
  }
  const displayName = requiredString(item, "display_name");
  const capabilities = stringArray(item, "capabilities");
  const command = stringArray(item, "command");
  if (command.length === 0) throw new Error("Adapter command cannot be empty");
  const args = stringArray(item, "args");
  validateArgs(args);
  const env = optionalStringRecord(item, "env");
  const resultMode = requiredString(item, "result_mode");
  if (resultMode !== "file" && resultMode !== "stdout_json") {
    throw new Error("result_mode must be file or stdout_json");
  }
  const timeoutSeconds = optionalPositiveNumber(item, "timeout_seconds");
  const cancelGraceSeconds = optionalNonNegativeNumber(
    item,
    "cancel_grace_seconds",
  );
  const session = validateSession(item.session, args);
  return {
    id,
    display_name: displayName,
    capabilities,
    command,
    args,
    ...(env ? { env } : {}),
    result_mode: resultMode,
    ...(timeoutSeconds !== undefined ? { timeout_seconds: timeoutSeconds } : {}),
    ...(cancelGraceSeconds !== undefined
      ? { cancel_grace_seconds: cancelGraceSeconds }
      : {}),
    ...(session ? { session } : {}),
  };
}

function validateSession(
  value: unknown,
  fallbackArgs: string[],
): AdapterSessionManifest | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new Error("session must be an object");
  }
  const item = value as Record<string, unknown>;
  if (item.resume !== true) return undefined;
  const resumeArgs = stringArray(item, "resume_args");
  if (resumeArgs.length === 0) {
    throw new Error("session.resume_args is required when session.resume=true");
  }
  validateArgs(resumeArgs);
  const newArgsValue = item.new_args;
  let newArgs = fallbackArgs;
  if (newArgsValue !== undefined) {
    newArgs = stringArray(item, "new_args");
    validateArgs(newArgs);
  }
  const resultField =
    typeof item.result_field === "string" && item.result_field.trim()
      ? item.result_field.trim()
      : "session_id";
  const discoveryGlob =
    typeof item.discovery_glob === "string" && item.discovery_glob.trim()
      ? item.discovery_glob.trim()
      : undefined;
  return {
    resume: true,
    new_args: newArgs,
    resume_args: resumeArgs,
    result_field: resultField,
    ...(discoveryGlob ? { discovery_glob: discoveryGlob } : {}),
  };
}

function validateArgs(args: string[]): void {
  for (const arg of args) {
    for (const match of arg.matchAll(/\{([a-z0-9_]+)\}/gu)) {
      const name = match[1];
      if (name && !KNOWN_PLACEHOLDERS.has(name)) {
        throw new Error(`Unknown placeholder {${name}} in adapter args`);
      }
    }
  }
}

function requiredString(item: Record<string, unknown>, name: string): string {
  const value = item[name];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`Adapter manifest ${name} is required`);
  }
  return value.trim();
}

function stringArray(item: Record<string, unknown>, name: string): string[] {
  const value = item[name];
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.some((entry) => typeof entry !== "string")) {
    throw new Error(`Adapter manifest ${name} must be an array of strings`);
  }
  return value as string[];
}

function optionalStringRecord(
  item: Record<string, unknown>,
  name: string,
): Record<string, string> | undefined {
  const value = item[name];
  if (value === undefined) return undefined;
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value) ||
    Object.values(value).some((entry) => typeof entry !== "string")
  ) {
    throw new Error(`Adapter manifest ${name} must be a string map`);
  }
  return value as Record<string, string>;
}

function optionalPositiveNumber(
  item: Record<string, unknown>,
  name: string,
): number | undefined {
  const value = item[name];
  if (value === undefined) return undefined;
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new Error(`Adapter manifest ${name} must be a positive number`);
  }
  return value;
}

function optionalNonNegativeNumber(
  item: Record<string, unknown>,
  name: string,
): number | undefined {
  const value = item[name];
  if (value === undefined) return undefined;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error(`Adapter manifest ${name} must be a non-negative number`);
  }
  return value;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
