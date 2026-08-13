import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";

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
