import { execFileSync } from "node:child_process";
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
import { dirname } from "node:path";

// @napi-rs/keyring loads a native DLL on Windows. Eagerly importing it at
// module scope pins node_modules files for the lifetime of the process, so
// a worker that loads the addon before applyPendingUpdate runs `npm ci`
// makes that install fail with EPERM (and on failure npm ci leaves the
// dependency tree half-deleted, crashing the next worker start). Resolve
// the addon lazily on first use instead: a freshly restarted worker then
// starts without pinning node_modules, so a deferred pending install can
// actually complete.
const require = createRequire(import.meta.url);

type KeyringModule = typeof import("@napi-rs/keyring");
let keyring: KeyringModule | undefined;
function loadKeyring(): KeyringModule {
  if (!keyring) keyring = require("@napi-rs/keyring") as KeyringModule;
  return keyring;
}

export function readSystemCredential(
  service: string | undefined,
  account: string | undefined,
  label: string,
): string | undefined {
  if (!service && !account) return undefined;
  if (!service || !account) {
    throw new Error(
      `${label} credential service and account must be configured together`,
    );
  }
  try {
    const { Entry } = loadKeyring();
    const password = new Entry(service, account).getPassword()?.trim();
    if (!password) throw new Error("credential is empty or missing");
    return password;
  } catch {
    throw new Error(`Could not load the ${label} credential from the system store`);
  }
}

export function writeSystemCredential(
  service: string,
  account: string,
  value: string,
  label: string,
): void {
  try {
    const { Entry } = loadKeyring();
    new Entry(service, account).setPassword(value);
  } catch {
    throw new Error(
      `Could not save the ${label} credential in the system store`,
    );
  }
}

export function deleteSystemCredential(
  service: string,
  account: string,
  label: string,
): void {
  try {
    const { Entry } = loadKeyring();
    new Entry(service, account).deletePassword();
  } catch {
    throw new Error(
      `Could not delete the ${label} credential from the system store`,
    );
  }
}

export function readLegacyMacKeychainCredential(
  service: string | undefined,
  account: string | undefined,
  label: string,
): string | undefined {
  if (!service && !account) return undefined;
  if (!service) throw new Error(`${label} Keychain service is required`);
  if (process.platform !== "darwin") {
    throw new Error(`${label} legacy Keychain references require macOS`);
  }
  const args = ["find-generic-password"];
  if (account) args.push("-a", account);
  args.push("-s", service, "-w");
  try {
    const password = execFileSync("/usr/bin/security", args, {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    if (!password) throw new Error("credential is empty");
    return password;
  } catch {
    throw new Error(`Could not load the ${label} credential from Keychain`);
  }
}

/**
 * Read the explicit file-backed credential fallback. The file is intentionally
 * a tiny dotenv subset so it can be inspected and rotated without adding a
 * second configuration format.
 */
export function readCredentialFile(
  path: string | undefined,
  key: string,
  label: string,
): string | undefined {
  if (!path || !existsSync(path)) return undefined;
  assertCredentialFileMode(path, label);
  const value = parseCredentialFile(readFileSync(path, "utf8"))[key];
  if (value === undefined || !value.trim()) return undefined;
  return value.trim();
}

/** Load all entries from an explicit credential file into the process env. */
export function readCredentialEnv(path: string): Record<string, string> {
  if (!existsSync(path)) return {};
  assertCredentialFileMode(path, "credential file");
  return parseCredentialFile(readFileSync(path, "utf8"));
}

/**
 * Save one credential atomically, retaining other entries in the file. This
 * function is only used after the user explicitly selected file storage.
 */
export function writeCredentialFile(
  path: string,
  key: string,
  value: string,
  label: string,
): void {
  if (!key || !/^[A-Z][A-Z0-9_]*$/u.test(key)) {
    throw new Error(`${label} credential key is invalid`);
  }
  if (/\r|\n/u.test(value)) {
    throw new Error(`${label} credential cannot contain newlines`);
  }
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  if (process.platform !== "win32") chmodSync(dirname(path), 0o700);
  const original = existsSync(path) ? readFileSync(path, "utf8") : "";
  const lines = original.split(/\r?\n/u);
  let replaced = false;
  const next = lines.map((line) => {
    const match = line.match(/^\s*([A-Z][A-Z0-9_]*)\s*=/u);
    if (!match || match[1] !== key) return line;
    replaced = true;
    return `${key}=${dotenvValue(value)}`;
  });
  if (!replaced) {
    if (next.at(-1)?.trim()) next.push("");
    next.push(`${key}=${dotenvValue(value)}`);
  }
  const temporary = `${path}.${process.pid}.tmp`;
  writeFileSync(temporary, `${next.join("\n").replace(/\n+$/u, "")}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  if (process.platform !== "win32") chmodSync(temporary, 0o600);
  renameSync(temporary, path);
  if (process.platform !== "win32") chmodSync(path, 0o600);
}

function parseCredentialFile(text: string): Record<string, string> {
  const values: Record<string, string> = {};
  for (const rawLine of text.split(/\r?\n/u)) {
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
    if (key) values[key] = value;
  }
  return values;
}

function dotenvValue(value: string): string {
  if (!value.includes('"')) return `"${value}"`;
  if (!value.includes("'")) return `'${value}'`;
  throw new Error("Credential values cannot contain both quote styles");
}

function assertCredentialFileMode(path: string, label: string): void {
  if (process.platform === "win32") return;
  let mode: number;
  try {
    mode = statSync(path).mode & 0o777;
  } catch {
    throw new Error(`Could not inspect the ${label}`);
  }
  if (mode !== 0o600) {
    throw new Error(
      `The ${label} must have permissions 0600; run chmod 600 ${path}`,
    );
  }
}
