import { existsSync, readFileSync } from "node:fs";
import { homedir, hostname, userInfo } from "node:os";
import { resolve } from "node:path";

import {
  readLegacyMacKeychainCredential,
  readSystemCredential,
} from "./credential-store.js";

export type RemoteToolPolicy = "no_tools" | "read_only" | "full";
export type WebSearchMode = "auto" | "disabled" | "deepseek";
export type WorkerSessionMode = "per_task" | "continuous";
export type RemotePiResourcePolicy =
  | "disabled"
  | "global"
  | "trusted_project";

export interface AgentHostConfig {
  hubEnabled: boolean;
  hubUrl?: string;
  hubToken?: string;
  hubUsername?: string;
  hubPassword?: string;
  hubNodeToken?: string;
  hubNodeTokenCacheDisabled?: boolean;
  principalId: string;
  principalDisplayName: string;
  actorId: string;
  actorDisplayName: string;
  nodeId: string;
  nodeDisplayName: string;
  workspaceRoot: string;
  sessionDir: string;
  pollSeconds: number;
  leaseSeconds: number;
  workerConcurrency: number;
  workerSupervised: boolean;
  workerSessionMode: WorkerSessionMode;
  workerSessionMaxTasks: number;
  workerSessionMaxAgeHours: number;
  remoteToolPolicy: RemoteToolPolicy;
  remotePiResourcePolicy: RemotePiResourcePolicy;
  selfUpdateEnabled: boolean;
  builtinCapabilitiesEnabled: boolean;
  subagentMaxDepth: number;
  subagentConcurrency: number;
  backgroundMaxProcesses: number;
  webSearchMode: WebSearchMode;
  webSearchModel: string;
  piProvider?: string;
  piModel?: string;
  remoteBaseUrl?: string;
  remoteApiKey?: string;
  remoteModel?: string;
  contextWindow: number;
  maxOutputTokens: number;
  thinkingLevel: string;
}

export function loadProjectEnv(path?: string): void {
  const envPath = path ? resolve(path) : discoverProjectEnv();
  if (!envPath || !existsSync(envPath)) return;
  for (const rawLine of readFileSync(envPath, "utf8").split(/\r?\n/u)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const separator = line.indexOf("=");
    const key = line.slice(0, separator).trim();
    let value = line.slice(separator + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (key && process.env[key] === undefined) process.env[key] = value;
  }
}

export function discoverProjectEnv(cwd = process.cwd()): string | undefined {
  return firstExisting([
    resolve(cwd, ".private/env/agent.env"),
    resolve(cwd, ".env.agent"),
    resolve(cwd, "../.private/env/agent.env"),
    resolve(cwd, "../.env.agent"),
    resolve(cwd, ".env"),
    resolve(cwd, "../.env"),
  ]);
}

/**
 * Canonical agent env file for a repository root. Prefers the private layout,
 * falls back to the legacy root-level `.env.agent`, and otherwise returns the
 * new canonical path so setup/connect can create it.
 */
export function agentEnvPath(root: string): string {
  return (
    firstExisting([
      resolve(root, ".private/env/agent.env"),
      resolve(root, ".env.agent"),
    ]) ?? resolve(root, ".private/env/agent.env")
  );
}

function firstExisting(paths: string[]): string | undefined {
  return paths.find((path) => existsSync(path));
}

function required(name: string, fallback?: string): string {
  const value = (process.env[name] ?? fallback ?? "").trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function positiveNumber(name: string, fallback: number): number {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`${name} must be a positive number`);
  }
  return value;
}

function nonNegativeNumber(name: string, fallback: number): number {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isFinite(value) || value < 0) {
    throw new Error(`${name} must be a non-negative number`);
  }
  return value;
}

function stableSlug(value: string): string {
  const slug = value.toLowerCase().replace(/[^a-z0-9._-]+/gu, "-");
  return slug.replace(/^-+|-+$/gu, "") || "node";
}

export function assertRemoteUrl(value: string): string {
  const url = new URL(value);
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error("Agent model URL must use HTTP or HTTPS");
  }
  const host = url.hostname.replace(/^\[|\]$/gu, "").toLowerCase();
  if (isBannedRemoteHost(host)) {
    throw new Error("Agent Host only accepts a remote model endpoint");
  }
  return value.replace(/\/$/u, "");
}

function isBannedRemoteHost(host: string): boolean {
  if (
    host === "localhost" ||
    host === "::1" ||
    host === "0.0.0.0" ||
    host.startsWith("127.") ||
    host.endsWith(".localhost")
  ) {
    return true;
  }
  const parts = host.split(".").map((part) => Number.parseInt(part, 10));
  if (parts.length !== 4 || parts.some((part) => Number.isNaN(part))) {
    return false;
  }
  const [a, b] = parts;
  if (a === undefined || b === undefined) return false;
  if (a === 10) return true;
  if (a === 192 && b === 168) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 169 && b === 254) return true;
  if (a === 100 && b >= 64 && b <= 127) return true;
  return false;
}

export function loadConfig(): AgentHostConfig {
  loadProjectEnv(process.env.AGENT_ENV_FILE);
  const host = stableSlug(hostname());
  const owner = stableSlug(userInfo().username);
  const remoteToolPolicy = (process.env.AGENT_REMOTE_TOOL_POLICY ??
    "full") as RemoteToolPolicy;
  if (!["no_tools", "read_only", "full"].includes(remoteToolPolicy)) {
    throw new Error(
      "AGENT_REMOTE_TOOL_POLICY must be no_tools, read_only, or full",
    );
  }
  const remotePiResourcePolicy = (process.env.AGENT_REMOTE_PI_RESOURCES ??
    "disabled") as RemotePiResourcePolicy;
  if (
    !["disabled", "global", "trusted_project"].includes(
      remotePiResourcePolicy,
    )
  ) {
    throw new Error(
      "AGENT_REMOTE_PI_RESOURCES must be disabled, global, or trusted_project",
    );
  }
  const selfUpdateEnabled = process.env.AGENT_SELF_UPDATE?.trim() !== "0";
  const workerSessionMode = (process.env.AGENT_WORKER_SESSION_MODE ??
    "per_task") as WorkerSessionMode;
  if (!(["per_task", "continuous"] as const).includes(workerSessionMode)) {
    throw new Error(
      "AGENT_WORKER_SESSION_MODE must be per_task or continuous",
    );
  }
  const webSearchMode = (process.env.AGENT_WEB_SEARCH ??
    "auto") as WebSearchMode;
  if (!["auto", "disabled", "deepseek"].includes(webSearchMode)) {
    throw new Error(
      "AGENT_WEB_SEARCH must be auto, disabled, or deepseek",
    );
  }
  const hubRuntimeDisabled = process.env.AGENT_HUB_RUNTIME_DISABLED === "1";
  const hubUrl = hubRuntimeDisabled
    ? undefined
    : process.env.AGENT_HUB_URL?.trim() || undefined;
  const hubToken = hubRuntimeDisabled
    ? undefined
    : process.env.AGENT_HUB_TOKEN?.trim() ||
      configuredCredential(
        process.env.AGENT_HUB_TOKEN_CREDENTIAL_SERVICE?.trim(),
        process.env.AGENT_HUB_TOKEN_CREDENTIAL_ACCOUNT?.trim(),
        process.env.AGENT_HUB_TOKEN_KEYCHAIN_SERVICE?.trim(),
        process.env.AGENT_HUB_TOKEN_KEYCHAIN_ACCOUNT?.trim(),
        "Hub",
      );
  const hubUsername = hubRuntimeDisabled
    ? undefined
    : process.env.AGENT_HUB_USERNAME?.trim() || undefined;
  const hubPassword = hubRuntimeDisabled
    ? undefined
    : process.env.AGENT_HUB_PASSWORD?.trim() ||
      optionalConfiguredCredential(
        process.env.AGENT_HUB_PASSWORD_CREDENTIAL_SERVICE?.trim(),
        process.env.AGENT_HUB_PASSWORD_CREDENTIAL_ACCOUNT?.trim(),
        process.env.AGENT_HUB_PASSWORD_KEYCHAIN_SERVICE?.trim(),
        process.env.AGENT_HUB_PASSWORD_KEYCHAIN_ACCOUNT?.trim(),
        "Hub password",
      );
  const hubNodeToken = hubRuntimeDisabled
    ? undefined
    : process.env.AGENT_HUB_NODE_TOKEN?.trim() ||
      optionalConfiguredCredential(
        process.env.AGENT_HUB_NODE_TOKEN_CREDENTIAL_SERVICE?.trim(),
        process.env.AGENT_HUB_NODE_TOKEN_CREDENTIAL_ACCOUNT?.trim(),
        process.env.AGENT_HUB_NODE_TOKEN_KEYCHAIN_SERVICE?.trim(),
        process.env.AGENT_HUB_NODE_TOKEN_KEYCHAIN_ACCOUNT?.trim(),
        "Hub node credential",
      );
  const hubConfig = resolveHubConfig(
    hubUrl,
    hubToken,
    hubUsername,
    hubPassword,
    hubNodeToken,
  );

  const piProvider = process.env.PI_PROVIDER?.trim() || undefined;
  const piModel = process.env.PI_MODEL?.trim() || undefined;
  if ((piProvider === undefined) !== (piModel === undefined)) {
    throw new Error("PI_PROVIDER and PI_MODEL must be configured together");
  }
  const rawBaseUrl =
    process.env.AGENT_REMOTE_BASE_URL?.trim() ||
    process.env.LLM_BASE_URL?.trim() ||
    undefined;
  const remoteBaseUrl = rawBaseUrl ? assertRemoteUrl(rawBaseUrl) : undefined;
  const remoteModel =
    process.env.AGENT_REMOTE_MODEL?.trim() ||
    process.env.LLM_MODEL?.trim() ||
    undefined;
  const remoteApiKey =
    process.env.AGENT_REMOTE_API_KEY?.trim() ||
    process.env.LLM_API_KEY?.trim() ||
    configuredCredential(
      process.env.AGENT_REMOTE_API_KEY_CREDENTIAL_SERVICE?.trim(),
      process.env.AGENT_REMOTE_API_KEY_CREDENTIAL_ACCOUNT?.trim(),
      process.env.AGENT_REMOTE_API_KEY_KEYCHAIN_SERVICE?.trim(),
      process.env.AGENT_REMOTE_API_KEY_KEYCHAIN_ACCOUNT?.trim(),
      "remote model",
    );
  if (!piProvider && (!remoteBaseUrl || !remoteModel)) {
    throw new Error(
      "Configure PI_PROVIDER/PI_MODEL or a remote LLM_BASE_URL/LLM_MODEL",
    );
  }

  const workspaceRoot = resolve(
    process.env.AGENT_WORKSPACE_ROOT?.trim() || process.cwd(),
  );
  return {
    ...hubConfig,
    ...(hubNodeToken ? { hubNodeToken } : {}),
    principalId: required("AGENT_PRINCIPAL_ID", `human-${owner}`),
    principalDisplayName: required(
      "AGENT_PRINCIPAL_NAME",
      userInfo().username,
    ),
    actorId: required("AGENT_ACTOR_ID", `pi-${host}`),
    actorDisplayName: required("AGENT_ACTOR_NAME", `Pi on ${hostname()}`),
    nodeId: required("AGENT_NODE_ID", host),
    nodeDisplayName: required("AGENT_NODE_NAME", hostname()),
    workspaceRoot,
    sessionDir: loadSessionDir(false),
    pollSeconds: Math.min(30, positiveNumber("AGENT_POLL_SECONDS", 20)),
    leaseSeconds: Math.min(900, positiveNumber("AGENT_LEASE_SECONDS", 300)),
    workerConcurrency: Math.min(
      16,
      Math.floor(positiveNumber("AGENT_WORKER_CONCURRENCY", 1)),
    ),
    workerSupervised: process.env.AGENT_WORKER_SUPERVISED === "1",
    workerSessionMode,
    workerSessionMaxTasks: Math.min(
      10_000,
      Math.floor(nonNegativeNumber("AGENT_WORKER_SESSION_MAX_TASKS", 0)),
    ),
    workerSessionMaxAgeHours: Math.min(
      24 * 365,
      nonNegativeNumber("AGENT_WORKER_SESSION_MAX_AGE_HOURS", 0),
    ),
    remoteToolPolicy,
    remotePiResourcePolicy,
    selfUpdateEnabled,
    builtinCapabilitiesEnabled:
      process.env.AGENT_BUILTIN_CAPABILITIES?.trim() !== "0",
    subagentMaxDepth: Math.min(
      4,
      Math.floor(positiveNumber("AGENT_SUBAGENT_MAX_DEPTH", 2)),
    ),
    subagentConcurrency: Math.min(
      8,
      Math.floor(positiveNumber("AGENT_SUBAGENT_CONCURRENCY", 4)),
    ),
    backgroundMaxProcesses: Math.min(
      32,
      Math.floor(positiveNumber("AGENT_BACKGROUND_MAX_PROCESSES", 8)),
    ),
    webSearchMode,
    webSearchModel:
      process.env.AGENT_WEB_SEARCH_MODEL?.trim() || "deepseek-v4-flash",
    ...(piProvider ? { piProvider } : {}),
    ...(piModel ? { piModel } : {}),
    ...(remoteBaseUrl ? { remoteBaseUrl } : {}),
    ...(remoteApiKey ? { remoteApiKey } : {}),
    ...(remoteModel ? { remoteModel } : {}),
    contextWindow: positiveNumber("AGENT_MODEL_CONTEXT_WINDOW", 128_000),
    maxOutputTokens: positiveNumber("AGENT_MODEL_MAX_TOKENS", 8_192),
    thinkingLevel:
      process.env.AGENT_THINKING_LEVEL?.trim() || "off",
  };
}

function configuredCredential(
  service: string | undefined,
  account: string | undefined,
  legacyService: string | undefined,
  legacyAccount: string | undefined,
  label: string,
): string | undefined {
  if (service || account) return readSystemCredential(service, account, label);
  return readLegacyMacKeychainCredential(legacyService, legacyAccount, label);
}

function optionalConfiguredCredential(
  service: string | undefined,
  account: string | undefined,
  legacyService: string | undefined,
  legacyAccount: string | undefined,
  label: string,
): string | undefined {
  try {
    return configuredCredential(
      service,
      account,
      legacyService,
      legacyAccount,
      label,
    );
  } catch {
    return undefined;
  }
}

export function resolveHubConfig(
  hubUrl?: string,
  hubToken?: string,
  hubUsername?: string,
  hubPassword?: string,
  hubNodeToken?: string,
): { hubEnabled: false } | {
  hubEnabled: true;
  hubUrl: string;
  hubToken?: string;
  hubUsername?: string;
  hubPassword?: string;
  hubNodeToken?: string;
} {
  const hasToken = Boolean(hubToken);
  const hasPassword = Boolean(hubUsername && hubPassword);
  const hasNodeToken = Boolean(hubNodeToken);
  const hasPartialUsername = Boolean(hubUsername && !hubPassword);
  if ((hubUrl === undefined && (hasToken || hasPassword || hasNodeToken || hasPartialUsername)) ||
      (hubUrl !== undefined && !hasToken && !hasPassword && !hasNodeToken && !hasPartialUsername)) {
    throw new Error(
      "AGENT_HUB_URL and Hub credentials must be configured together",
    );
  }
  if (!hubUrl) return { hubEnabled: false };
  if (hasToken && !hasPassword && !hasNodeToken) {
    console.warn(
      "Hub token mode is deprecated; register a password account and run `agent setup` to switch.",
    );
  }
  return {
    hubEnabled: true,
    hubUrl: assertHttpUrl(hubUrl, "AGENT_HUB_URL"),
    ...(hasToken ? { hubToken: secureHubToken(hubToken!) } : {}),
    ...(hubUsername ? { hubUsername } : {}),
    ...(hasPassword ? { hubPassword: hubPassword! } : {}),
    ...(hasNodeToken ? { hubNodeToken: hubNodeToken! } : {}),
  };
}

function assertHttpUrl(value: string, name: string): string {
  const url = new URL(value);
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error(`${name} must use HTTP or HTTPS`);
  }
  return value.replace(/\/$/u, "");
}

export function loadSessionDir(loadEnv = true): string {
  if (loadEnv) loadProjectEnv(process.env.AGENT_ENV_FILE);
  return resolve(
    process.env.AGENT_SESSION_DIR?.trim() ||
      resolve(homedir(), ".pi", "agent", "sessions"),
  );
}

function secureHubToken(value: string): string {
  if (value.length < 24) {
    throw new Error("AGENT_HUB_TOKEN must contain at least 24 characters");
  }
  return value;
}
