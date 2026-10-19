import { createRequire } from "node:module";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { homedir, userInfo } from "node:os";
import { dirname, join, resolve } from "node:path";

export type ConfigSource = "process" | "explicit" | "dsh" | "legacy" | "none";
export type CredentialBackend = "env" | "file" | "keychain" | "missing";

export interface AgentConfigOptions {
  dshHome?: string;
  configPath?: string;
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  legacyPaths?: string[];
}

export interface AgentConfig {
  values: Record<string, string>;
  path?: string;
  source: ConfigSource;
  dshHome: string;
  canonicalPath: string;
  legacyPaths: string[];
}

export interface CredentialResolution {
  value?: string;
  backend: CredentialBackend;
  persisted: boolean;
  source?: string;
  error?: string;
}

export interface CredentialOptions extends AgentConfigOptions {
  config?: AgentConfig;
  backend?: "auto" | CredentialBackend;
  filePath?: string;
  service?: string;
  account?: string;
  label?: string;
  allowKeychain?: boolean;
}

export interface CredentialWriteResult {
  backend: Exclude<CredentialBackend, "missing">;
  persisted: boolean;
  path?: string;
}

const require = createRequire(import.meta.url);
const SECRET_KEY = /(?:API_KEY|PASSWORD|TOKEN|SECRET)(?:$|_)/u;
const PROCESS_KEY = /^(?:AGENT_|DSH_|PI_|LLM_)/u;

export function parseEnv(text: string): Record<string, string> {
  const values: Record<string, string> = {};
  for (const raw of text.split(/\r?\n/u)) {
    const line = raw.trim();
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
    if (key) values[key] = value;
  }
  return values;
}

export function defaultDshHome(env: NodeJS.ProcessEnv = process.env): string {
  return resolve(env.DSH_HOME?.trim() || join(homedir(), ".dsh"));
}

export function canonicalConfigPath(dshHome = defaultDshHome()): string {
  return join(resolve(dshHome), ".env");
}

export function legacyConfigPaths(
  cwd = process.cwd(),
  env: NodeJS.ProcessEnv = process.env,
): string[] {
  const configHome =
    process.platform === "win32"
      ? env.APPDATA?.trim() || join(homedir(), "AppData", "Roaming")
      : env.XDG_CONFIG_HOME?.trim() || join(homedir(), ".config");
  return [
    join(configHome, "agentsociety", "agent.env"),
    resolve(cwd, ".private", "env", "agent.env"),
    resolve(cwd, ".env.agent"),
    resolve(cwd, "..", ".private", "env", "agent.env"),
    resolve(cwd, "..", ".env.agent"),
  ];
}

/**
 * Load shared AgentSociety configuration without exposing secret values.
 * File precedence is legacy < $DSH_HOME/.env < explicit file < process env.
 */
export function loadAgentConfig(
  options: AgentConfigOptions = {},
): AgentConfig {
  const env = options.env ?? process.env;
  const dshHome = resolve(options.dshHome?.trim() || defaultDshHome(env));
  const canonicalPath = canonicalConfigPath(dshHome);
  const explicitPath =
    options.configPath?.trim() || env.AGENT_ENV_FILE?.trim() || undefined;
  const legacy = options.legacyPaths ?? legacyConfigPaths(options.cwd, env);
  const values: Record<string, string> = {};
  let selectedPath: string | undefined;
  let selectedSource: ConfigSource = "none";

  for (const path of [...legacy].reverse()) {
    mergeFile(values, path);
    if (!selectedPath && existsSync(path)) {
      selectedPath = path;
      selectedSource = "legacy";
    }
  }
  if (existsSync(canonicalPath)) {
    mergeFile(values, canonicalPath);
    selectedPath = canonicalPath;
    selectedSource = "dsh";
  }
  if (explicitPath) {
    mergeFile(values, resolve(explicitPath));
    if (existsSync(explicitPath)) {
      selectedPath = resolve(explicitPath);
      selectedSource = "explicit";
    }
  }
  for (const [key, value] of Object.entries(env)) {
    if (PROCESS_KEY.test(key) && value !== undefined) values[key] = value;
  }
  const hasProcessConfig = Object.keys(env).some(
    (key) => PROCESS_KEY.test(key) && env[key] !== undefined,
  );
  if (hasProcessConfig && selectedSource === "none") selectedSource = "process";
  return {
    values,
    ...(selectedPath ? { path: selectedPath } : {}),
    source: selectedSource,
    dshHome,
    canonicalPath,
    legacyPaths: legacy,
  };
}

export function writeAgentConfig(
  values: Record<string, string | null | undefined>,
  options: AgentConfigOptions & { path?: string } = {},
): string {
  const config = loadAgentConfig(options);
  const path = resolve(
    options.path ||
      options.configPath ||
      options.env?.AGENT_ENV_FILE ||
      config.canonicalPath,
  );
  const current = existsSync(path) ? readFileSync(path, "utf8") : "";
  const update: Record<string, string | null> = {};
  for (const [key, value] of Object.entries(values)) {
    if (!/^AGENT_[A-Z0-9_]+$/u.test(key)) continue;
    if (
      SECRET_KEY.test(key) &&
      !key.endsWith("_CREDENTIAL_SERVICE") &&
      !key.endsWith("_CREDENTIAL_ACCOUNT") &&
      !key.endsWith("_KEYCHAIN_SERVICE") &&
      !key.endsWith("_KEYCHAIN_ACCOUNT") &&
      key !== "AGENT_CREDENTIAL_BACKEND" &&
      key !== "AGENT_CREDENTIAL_FILE"
    ) {
      continue;
    }
    update[key] = value ?? null;
  }
  const next = updateEnv(current, update);
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  if (process.platform !== "win32") chmodSync(dirname(path), 0o700);
  const temporary = path + "." + process.pid + ".tmp";
  writeFileSync(temporary, next, { encoding: "utf8", mode: 0o600 });
  if (process.platform !== "win32") chmodSync(temporary, 0o600);
  renameSync(temporary, path);
  if (process.platform !== "win32") chmodSync(path, 0o600);
  return path;
}

export function resolveCredential(
  name: string,
  options: CredentialOptions = {},
): CredentialResolution {
  const config = options.config ?? loadAgentConfig(options);
  const env = options.env ?? process.env;
  const direct = env[name]?.trim();
  if (direct) {
    return {
      value: direct,
      backend: "env",
      persisted: false,
      source: "process",
    };
  }
  const filePath =
    options.filePath ||
    config.values.AGENT_CREDENTIAL_FILE?.trim() ||
    env.AGENT_CREDENTIAL_FILE?.trim();
  if (filePath) {
    try {
      const value = readCredentialFile(resolve(filePath), name);
      if (value) {
        return {
          value,
          backend: "file",
          persisted: true,
          source: resolve(filePath),
        };
      }
    } catch (error) {
      return {
        backend: "file",
        persisted: false,
        source: resolve(filePath),
        error: errorMessage(error),
      };
    }
  }
  const mergedValue = config.values[name]?.trim();
  if (mergedValue) {
    return {
      value: mergedValue,
      backend: "file",
      persisted: true,
      ...(config.path ? { source: config.path } : {}),
    };
  }
  const backend =
    options.backend ??
    config.values.AGENT_CREDENTIAL_BACKEND ??
    "auto";
  if (
    backend !== "env" &&
    backend !== "file" &&
    options.allowKeychain !== false
  ) {
    const reference = credentialReference(name, config, options);
    if (reference) {
      try {
        const value = readKeychain(reference.service, reference.account);
        if (value) {
          return {
            value,
            backend: "keychain",
            persisted: true,
            source: reference.service,
          };
        }
      } catch (error) {
        return {
          backend: "keychain",
          persisted: false,
          source: reference.service,
          error: errorMessage(error),
        };
      }
    }
  }
  return { backend: "missing", persisted: false };
}

export function writeCredential(
  name: string,
  value: string,
  options: CredentialOptions = {},
): CredentialWriteResult {
  if (!/^[A-Z][A-Z0-9_]*$/u.test(name)) {
    throw new Error("Invalid credential name");
  }
  if (!value || /\r|\n/u.test(value)) {
    throw new Error("Credential value is invalid");
  }
  const config = options.config ?? loadAgentConfig(options);
  const backend =
    options.backend ??
    config.values.AGENT_CREDENTIAL_BACKEND ??
    (config.values.AGENT_CREDENTIAL_FILE ? "file" : "auto");
  if (backend === "env") {
    process.env[name] = value;
    return { backend: "env", persisted: false };
  }
  const filePath =
    options.filePath ||
    config.values.AGENT_CREDENTIAL_FILE?.trim() ||
    join(config.dshHome, "credentials.env");
  if (backend === "file") {
    writeCredentialFile(resolve(filePath), name, value);
    return { backend: "file", persisted: true, path: resolve(filePath) };
  }
  const reference = credentialReference(name, config, options);
  if (reference) {
    try {
      writeKeychain(reference.service, reference.account, value);
      return { backend: "keychain", persisted: true };
    } catch (error) {
      if (backend === "keychain") throw error;
    }
  }
  process.env[name] = value;
  return { backend: "env", persisted: false };
}

export function describeConfig(
  config: AgentConfig = loadAgentConfig(),
): Record<string, unknown> {
  const credentialFile =
    config.values.AGENT_CREDENTIAL_FILE?.trim() || undefined;
  const backend =
    config.values.AGENT_CREDENTIAL_BACKEND ||
    (credentialFile ? "file" : "auto");
  return {
    dshHome: config.dshHome,
    canonicalPath: config.canonicalPath,
    effectivePath: config.path,
    source: config.source,
    legacyPaths: config.legacyPaths,
    credentialBackend: backend,
    credentialFile,
    credentialPersisted: backend === "file" || backend === "keychain",
    configured: Boolean(
      config.values.AGENT_DSH_MODEL ||
        config.values.AGENT_REMOTE_MODEL ||
        config.values.LLM_MODEL ||
        config.values.PI_MODEL,
    ),
  };
}

function mergeFile(values: Record<string, string>, path: string): void {
  if (!existsSync(path)) return;
  Object.assign(values, parseEnv(readFileSync(path, "utf8")));
}

function updateEnv(
  text: string,
  update: Record<string, string | null>,
): string {
  const seen = new Set<string>();
  const lines = text ? text.split(/\r?\n/u) : [];
  const next = lines.map((line) => {
    const match = line.match(/^\s*([A-Z][A-Z0-9_]*)\s*=/u);
    if (!match) return line;
    const key = match[1];
    if (!key) return line;
    if (!(key in update)) return line;
    seen.add(key);
    const value = update[key];
    if (value === undefined) return line;
    // Preserve an explicit empty tombstone so the canonical DSH_HOME layer
    // can mask a value that remains in a legacy project file.
    return value === null ? key + "=\"\"" : key + "=" + dotenvValue(value);
  });
  for (const [key, value] of Object.entries(update)) {
    if (seen.has(key)) continue;
    if (next.at(-1)?.trim()) next.push("");
    next.push(key + "=" + (value === null ? '""' : dotenvValue(value)));
  }
  return next.join("\n").replace(/\n+$/u, "") + "\n";
}

function readCredentialFile(path: string, key: string): string | undefined {
  if (!existsSync(path)) return undefined;
  assertPrivateFile(path);
  return parseEnv(readFileSync(path, "utf8"))[key]?.trim() || undefined;
}

function writeCredentialFile(path: string, key: string, value: string): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  if (process.platform !== "win32") chmodSync(dirname(path), 0o700);
  const current = existsSync(path) ? readFileSync(path, "utf8") : "";
  const next = updateEnv(current, { [key]: value });
  const temporary = path + "." + process.pid + ".tmp";
  writeFileSync(temporary, next, { encoding: "utf8", mode: 0o600 });
  if (process.platform !== "win32") chmodSync(temporary, 0o600);
  renameSync(temporary, path);
  if (process.platform !== "win32") chmodSync(path, 0o600);
}

function assertPrivateFile(path: string): void {
  if (process.platform === "win32") return;
  const mode = statSync(path).mode & 0o777;
  if (mode !== 0o600) throw new Error(path + " must have permissions 0600");
}

interface CredentialReference {
  service: string;
  account: string;
}

function credentialReference(
  name: string,
  config: AgentConfig,
  options: CredentialOptions,
): CredentialReference | undefined {
  const configuredService =
    options.service ||
    config.values[name + "_CREDENTIAL_SERVICE"] ||
    config.values[name + "_KEYCHAIN_SERVICE"];
  const configuredAccount =
    options.account ||
    config.values[name + "_CREDENTIAL_ACCOUNT"] ||
    config.values[name + "_KEYCHAIN_ACCOUNT"];
  const configuredBackend =
    options.backend || config.values.AGENT_CREDENTIAL_BACKEND;
  if (!configuredService && !configuredAccount && configuredBackend !== "keychain") {
    return undefined;
  }
  const service =
    configuredService ||
    defaultService(name);
  const account =
    configuredAccount ||
    config.values.AGENT_CREDENTIAL_ACCOUNT ||
    userInfo().username;
  return service && account ? { service, account } : undefined;
}

function defaultService(name: string): string {
  const label = name
    .replace(/^AGENT_/u, "")
    .replaceAll("_", " ")
    .toLowerCase();
  return "AgentSociety " + label;
}

interface KeyringEntry {
  getPassword(): string | undefined;
  setPassword(value: string): void;
}

interface KeyringModule {
  Entry: new (service: string, account: string) => KeyringEntry;
}

function keyringModule(): KeyringModule {
  return require("@napi-rs/keyring") as KeyringModule;
}

function readKeychain(service: string, account: string): string | undefined {
  const Entry = keyringModule().Entry;
  return new Entry(service, account).getPassword()?.trim();
}

function writeKeychain(
  service: string,
  account: string,
  value: string,
): void {
  const Entry = keyringModule().Entry;
  new Entry(service, account).setPassword(value);
}

function dotenvValue(value: string): string {
  if (!value.includes('"')) return '"' + value + '"';
  if (!value.includes("'")) return "'" + value + "'";
  throw new Error("Values cannot contain both quote styles");
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
