import { existsSync, readFileSync } from "node:fs";
import { homedir, hostname, userInfo } from "node:os";
import { resolve } from "node:path";

export type RemoteToolPolicy = "no_tools" | "read_only" | "full";

export interface AgentHostConfig {
  hubUrl: string;
  hubToken: string;
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
  remoteToolPolicy: RemoteToolPolicy;
  piProvider?: string;
  piModel?: string;
  remoteBaseUrl?: string;
  remoteApiKey?: string;
  remoteApiKeyKeychainService?: string;
  remoteApiKeyKeychainAccount?: string;
  remoteModel?: string;
  contextWindow: number;
  maxOutputTokens: number;
}

export function loadProjectEnv(path = resolve(process.cwd(), "../.env")): void {
  if (!existsSync(path)) return;
  for (const rawLine of readFileSync(path, "utf8").split(/\r?\n/u)) {
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
  if (
    host === "localhost" ||
    host === "::1" ||
    host === "0.0.0.0" ||
    host.startsWith("127.")
  ) {
    throw new Error("Agent Host only accepts a remote model endpoint");
  }
  return value.replace(/\/$/u, "");
}

export function loadConfig(): AgentHostConfig {
  loadProjectEnv(process.env.AGENT_ENV_FILE);
  const host = stableSlug(hostname());
  const owner = stableSlug(userInfo().username);
  const remoteToolPolicy = (process.env.AGENT_REMOTE_TOOL_POLICY ??
    "read_only") as RemoteToolPolicy;
  if (!["no_tools", "read_only", "full"].includes(remoteToolPolicy)) {
    throw new Error(
      "AGENT_REMOTE_TOOL_POLICY must be no_tools, read_only, or full",
    );
  }

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
    undefined;
  const remoteApiKeyKeychainService =
    process.env.AGENT_REMOTE_API_KEY_KEYCHAIN_SERVICE?.trim() || undefined;
  const remoteApiKeyKeychainAccount =
    process.env.AGENT_REMOTE_API_KEY_KEYCHAIN_ACCOUNT?.trim() || undefined;
  if (!piProvider && (!remoteBaseUrl || !remoteModel)) {
    throw new Error(
      "Configure PI_PROVIDER/PI_MODEL or a remote LLM_BASE_URL/LLM_MODEL",
    );
  }

  const workspaceRoot = resolve(
    process.env.AGENT_WORKSPACE_ROOT?.trim() || process.cwd(),
  );
  return {
    hubUrl: required("AGENT_HUB_URL", "http://127.0.0.1:8080").replace(
      /\/$/u,
      "",
    ),
    hubToken: required("AGENT_HUB_TOKEN", process.env.BOT_API_TOKEN),
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
    sessionDir: resolve(
      process.env.AGENT_SESSION_DIR?.trim() ||
        resolve(homedir(), ".pi", "agent", "sessions"),
    ),
    pollSeconds: Math.min(30, positiveNumber("AGENT_POLL_SECONDS", 20)),
    leaseSeconds: Math.min(900, positiveNumber("AGENT_LEASE_SECONDS", 300)),
    remoteToolPolicy,
    ...(piProvider ? { piProvider } : {}),
    ...(piModel ? { piModel } : {}),
    ...(remoteBaseUrl ? { remoteBaseUrl } : {}),
    ...(remoteApiKey ? { remoteApiKey } : {}),
    ...(remoteApiKeyKeychainService
      ? { remoteApiKeyKeychainService }
      : {}),
    ...(remoteApiKeyKeychainAccount
      ? { remoteApiKeyKeychainAccount }
      : {}),
    ...(remoteModel ? { remoteModel } : {}),
    contextWindow: positiveNumber("AGENT_MODEL_CONTEXT_WINDOW", 128_000),
    maxOutputTokens: positiveNumber("AGENT_MODEL_MAX_TOKENS", 8_192),
  };
}
