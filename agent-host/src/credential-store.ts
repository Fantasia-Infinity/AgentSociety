import { Entry } from "@napi-rs/keyring";
import { execFileSync } from "node:child_process";

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
