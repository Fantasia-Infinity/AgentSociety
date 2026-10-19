import { accessSync, constants, existsSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { AgentHostConfig } from "./config.js";
import { defaultAgentConfigPath } from "./config.js";
import type { HubClient } from "./hub-client.js";
import { PiAgentEngine } from "./pi-engine.js";
import { createWebSearchProvider, webSearchStatus } from "./web-search.js";

export interface DoctorOptions {
  live?: boolean;
  json?: boolean;
  hubError?: unknown;
}

interface DoctorCheck {
  name: string;
  status: "ok" | "warn" | "fail";
  detail: string;
  fix?: string;
}

export async function runDoctor(
  config: AgentHostConfig,
  hub?: HubClient,
  options: DoctorOptions = {},
): Promise<void> {
  const checks: DoctorCheck[] = [];
  const add = (
    name: string,
    status: DoctorCheck["status"],
    detail: string,
    fix?: string,
  ) => {
    checks.push({ name, status, detail, ...(fix ? { fix } : {}) });
  };
  const expectedConfig = defaultAgentConfigPath();
  const configPath = config.configPath;
  const configExists = configPath !== undefined && existsSync(configPath);
  const legacyConfig =
    configPath !== undefined &&
    configExists &&
    configPath !== expectedConfig &&
    (configPath.endsWith("/.env.agent") ||
      configPath.endsWith("/.private/env/agent.env") ||
      configPath.endsWith("\\.env.agent") ||
      configPath.endsWith("\\.private\\env\\agent.env"));

  if (configExists && configPath && legacyConfig) {
    add(
      "configuration path",
      "warn",
      `${configPath} (legacy project config)`,
      "agent setup",
    );
  } else if (configExists && configPath) {
    const mode = fileMode(configPath);
    add(
      "configuration path",
      mode === "warn" ? "warn" : "ok",
      `${configPath}${mode === "warn" ? " (expected permissions 0600)" : ""}`,
      mode === "warn" ? `chmod 600 ${quoteShell(configPath)}` : undefined,
    );
  } else {
    add(
      "configuration path",
      "fail",
      `not configured; expected ${expectedConfig}`,
      "agent setup --mode local",
    );
  }

  const modelConfigured = Boolean(
    (config.piProvider && config.piModel) ||
      (config.remoteBaseUrl && config.remoteModel) ||
      process.env.AGENT_DSH_CONFIG?.trim() ||
      process.env.AGENT_DSH_RUNTIME_BIN?.trim(),
  );
  if (modelConfigured) {
    const model = config.remoteModel ?? config.piModel ?? config.dshModel ?? "configured runtime";
    add("model configuration", "ok", `${config.piProvider ?? "remote"} / ${model}`);
  } else {
    add(
      "model configuration",
      "fail",
      "no model provider, endpoint, or dsh runtime is configured",
      "agent setup --mode local",
    );
  }

  const backend = config.credentialBackend ?? "keychain";
  if (backend === "file") {
    const path = config.credentialFile ?? "configured credential file";
    const mode = config.credentialFile ? fileMode(config.credentialFile) : "missing";
    const fileError = config.credentialFileError;
    add(
      "credential backend",
      fileError || mode === "missing" || mode === "warn" ? "fail" : "warn",
      fileError || mode === "missing"
        ? fileError ?? `credential file is missing: ${path}`
        : mode === "warn"
          ? `credential file has unsafe permissions: ${path}`
          : `explicit file storage: ${path} (0600; security warning)`,
      fileError || mode === "missing"
        ? "agent setup --credentials file"
        : mode === "warn"
          ? `chmod 600 ${quoteShell(path)}`
          : "agent setup --credentials auto  # switch back to the system store",
    );
  } else if (backend === "env") {
    add(
      "credential backend",
      "warn",
      "environment-only; secrets are not persisted",
      "export AGENT_REMOTE_API_KEY before starting AgentSociety",
    );
  } else {
    add(
      "credential backend",
      config.remoteApiKey || config.piProvider ? "ok" : "warn",
      config.remoteApiKey || config.piProvider
        ? "system credential store / existing provider"
        : "system credential store selected, but no model credential was found",
      config.remoteApiKey || config.piProvider
        ? undefined
        : "agent setup",
    );
  }

  if (config.workspaceRoot && existsSync(config.workspaceRoot)) {
    let writable = true;
    try {
      accessSync(config.workspaceRoot, constants.R_OK | constants.W_OK);
    } catch {
      writable = false;
    }
    add(
      "workspace",
      writable ? "ok" : "fail",
      config.workspaceRoot,
      writable
        ? undefined
        : `chmod u+rwX ${quoteShell(config.workspaceRoot)}`,
    );
  } else {
    add(
      "workspace",
      "fail",
      `missing: ${config.workspaceRoot}`,
      `mkdir -p ${quoteShell(config.workspaceRoot)}`,
    );
  }

  if (existsSync(config.sessionDir)) {
    let writable = true;
    try {
      accessSync(config.sessionDir, constants.R_OK | constants.W_OK);
    } catch {
      writable = false;
    }
    add(
      "session directory",
      writable ? "ok" : "fail",
      config.sessionDir,
      writable
        ? undefined
        : `chmod u+rwX ${quoteShell(config.sessionDir)}`,
    );
  } else {
    add(
      "session directory",
      "warn",
      `will be created on first run: ${config.sessionDir}`,
      `mkdir -m 700 -p ${quoteShell(config.sessionDir)}`,
    );
  }

  add(
    "permissions",
    "ok",
    `remote=${config.remoteToolPolicy}; dsh=${config.dshPermissionMode ?? "read-only"}`,
  );
  add(
    "worker mode",
    config.hubReceiveDisabled ? "warn" : "ok",
    config.hubReceiveDisabled
      ? "dispatch-only; this host will not receive remote tasks"
      : config.hubEnabled
        ? "Hub worker reception enabled"
        : "local mode; Hub reception disabled",
    config.hubReceiveDisabled ? "agent setup --mode hub-worker" : undefined,
  );

  if (config.hubEnabled && config.hubUrl) {
    add("Hub configuration", "ok", config.hubUrl);
    if (config.hubNodeToken || config.hubToken) {
      add("Hub node credential", "ok", "present");
    } else if (config.hubUsername && config.hubPassword) {
      add(
        "Hub node credential",
        "warn",
        "account configured; this node has not been registered yet",
        "agent connect",
      );
    } else {
      add(
        "Hub node credential",
        "fail",
        "Hub URL is present but no account or node credential is available",
        "agent setup --mode hub-worker",
      );
    }
  } else {
    add("Hub configuration", "ok", "disabled (local Agent mode)");
  }

  const dshHome = resolve(process.env.DSH_HOME?.trim() || join(homedir(), ".dsh"));
  const metricsPath = join(dshHome, "agent-society-context-metrics.json");
  const metrics = readMetrics(metricsPath);
  if (metrics) {
    const sessions =
      metrics.contextUpdatesPerSession &&
      typeof metrics.contextUpdatesPerSession === "object"
        ? Object.keys(metrics.contextUpdatesPerSession).length
        : 0;
    add(
      "context/cache metrics",
      "ok",
      `initialBytes=${numberMetric(metrics.contextInitialBytes)}; ` +
        `deltaBytes=${numberMetric(metrics.contextDeltaBytes)}; ` +
        `snapshotBytes=${numberMetric(metrics.contextSnapshotBytes)}; ` +
        `sessions=${sessions}; ` +
        `digestCalls=${numberMetric(metrics.digestModelCalls)}; ` +
        `deduplicated=${numberMetric(metrics.digestDeduplicatedCalls)}; ` +
        `hubSchema=${typeof metrics.hubToolSchemaHash === "string" ? metrics.hubToolSchemaHash : "none"}`,
    );
  } else {
    add(
      "context/cache metrics",
      "warn",
      `not recorded yet: ${metricsPath}`,
      "start dsh once, then rerun agent doctor --json",
    );
  }
  const dshCommand = config.dshCommand?.[0] ?? "dsh";
  add(
    "dsh runtime",
    executableInPath(dshCommand) ? "ok" : "warn",
    executableInPath(dshCommand) ? dshCommand : `${dshCommand} not found on PATH`,
    executableInPath(dshCommand) ? undefined : "agent doctor after installing the combo runtime",
  );
  add(
    "worker profile",
    existsSync(join(dshHome, "profiles", config.dshPluginProfile ?? "agent-society-worker", "package.json"))
      ? "ok"
      : "warn",
    existsSync(join(dshHome, "profiles", config.dshPluginProfile ?? "agent-society-worker", "package.json"))
      ? config.dshPluginProfile ?? "agent-society-worker"
      : "dsh worker profile is missing",
    existsSync(join(dshHome, "profiles", config.dshPluginProfile ?? "agent-society-worker", "package.json"))
      ? undefined
      : "agent doctor from the combo install root",
  );
  add(
    "web profile",
    existsSync(join(dshHome, "profiles", config.dshWebProfile ?? "agent-society-web", "package.json"))
      ? "ok"
      : "warn",
    existsSync(join(dshHome, "profiles", config.dshWebProfile ?? "agent-society-web", "package.json"))
      ? config.dshWebProfile ?? "agent-society-web"
      : "dsh web profile is missing",
    existsSync(join(dshHome, "profiles", config.dshWebProfile ?? "agent-society-web", "package.json"))
      ? undefined
      : "agent web",
  );

  let searchStatus = "unavailable";
  try {
    searchStatus = webSearchStatus(
      config.webSearchMode,
      createWebSearchProvider(config),
    );
  } catch (error) {
    searchStatus = error instanceof Error ? error.message : String(error);
  }
  add("web search", "ok", searchStatus);
  add(
    "built-in tools",
    "ok",
    config.builtinCapabilitiesEnabled ? "enabled" : "disabled",
  );

  if (options.live) {
    if (config.hubEnabled) {
      if (hub) {
        try {
          await hub.listActors();
          add("Hub live check", "ok", `authenticated as ${config.actorId}`);
        } catch (error) {
          add(
            "Hub live check",
            "fail",
            error instanceof Error ? error.message : String(error),
            "agent connect",
          );
        }
      } else {
        add(
          "Hub live check",
          "fail",
          formatError(options.hubError ?? "could not create a Hub client"),
          "agent connect",
        );
      }
    } else {
      add("Hub live check", "ok", "skipped (local mode)");
    }

    try {
      const engine = await PiAgentEngine.create(config, hub);
      const conversation = await engine.createConversation({
        cwd: config.workspaceRoot,
        mode: "diagnostic",
        persisted: false,
      });
      try {
        const response = await conversation.prompt(
          "Connection check only. Reply with exactly AGENT_SETUP_OK and do not call tools.",
        );
        if (!response.text.trim()) {
          throw new Error("the remote model returned an empty response");
        }
        add("model live check", "ok", "diagnostic response received");
      } finally {
        await conversation.dispose();
      }
    } catch (error) {
      add(
        "model live check",
        "fail",
        formatError(error),
        "agent setup followed by agent doctor --live",
      );
    }

    if (config.dshWebEnabled) {
      const target = process.env.AGENT_DSH_WEB_TARGET?.trim() || "http://127.0.0.1:3080";
      try {
        const response = await fetch(target, {
          signal: AbortSignal.timeout(5_000),
        });
        add(
          "Web Bridge live check",
          response.ok ? "ok" : "warn",
          `${target} -> HTTP ${response.status}`,
          response.ok ? undefined : "agent web",
        );
      } catch (error) {
        add(
          "Web Bridge live check",
          "fail",
          formatError(error),
          "agent web",
        );
      }
    } else {
      add("Web Bridge live check", "ok", "disabled");
    }
  }

  if (options.json) {
    console.log(
      JSON.stringify(
        {
          ok: checks.every((check) => check.status !== "fail"),
          live: Boolean(options.live),
          checks,
        },
        null,
        2,
      ),
    );
  } else {
    for (const check of checks) {
      console.log(
        `[${check.status}] ${check.name}: ${check.detail}${check.fix ? ` — next: ${check.fix}` : ""}`,
      );
    }
    console.log(
      checks.some((check) => check.status === "fail")
        ? "\nDoctor found failures."
        : "\nDoctor checks passed (warnings may need attention).",
    );
  }
  if (checks.some((check) => check.status === "fail")) process.exitCode = 1;
}

function readMetrics(path: string): Record<string, any> | undefined {
  try {
    const value = JSON.parse(readFileSync(path, "utf8"));
    return value && typeof value === "object" && value.version === 1
      ? value
      : undefined;
  } catch {
    return undefined;
  }
}

function numberMetric(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "0";
}

function fileMode(path: string | undefined): "ok" | "warn" | "missing" {
  if (!path || !existsSync(path)) return "missing";
  if (process.platform === "win32") return "ok";
  try {
    return (statSync(path).mode & 0o777) === 0o600 ? "ok" : "warn";
  } catch {
    return "missing";
  }
}

function executableInPath(command: string): boolean {
  if (command.includes("/") || command.includes("\\")) {
    return isExecutable(command);
  }
  const separator = process.platform === "win32" ? ";" : ":";
  const candidates =
    process.platform === "win32" && !/\.(?:cmd|exe|bat)$/iu.test(command)
      ? [command, `${command}.cmd`, `${command}.exe`, `${command}.bat`]
      : [command];
  for (const entry of (process.env.PATH ?? "").split(separator)) {
    if (entry && candidates.some((candidate) => isExecutable(join(entry, candidate)))) {
      return true;
    }
  }
  return false;
}

function isExecutable(path: string): boolean {
  if (!existsSync(path)) return false;
  if (process.platform === "win32") return true;
  try {
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function quoteShell(value: string): string {
  return "'" + value.replace(/'/gu, "'\\''") + "'";
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const _sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
