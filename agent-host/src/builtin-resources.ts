import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const MANAGED_CHANNEL_MARKER = "agent-society-channel-v1";

export interface BuiltinResourceDefaults {
  extensionPaths: string[];
  diagnostics: string[];
}

export function ensureBuiltinResourceDefaults(): BuiltinResourceDefaults {
  const diagnostics: string[] = [];
  const extensionPaths = resolveBuiltinExtensionPaths();
  ensureLspDefaults(diagnostics);
  ensureChannelMcpDefaults(diagnostics);
  return { extensionPaths, diagnostics };
}

function resolveBuiltinExtensionPaths(): string[] {
  const require = createRequire(import.meta.url);
  const mcpEntry = require.resolve("pi-mcp-adapter");
  const lspPackage = require.resolve("pi-lsp-adapter/package.json");
  return [mcpEntry, join(dirname(lspPackage), "src", "index.ts")];
}

function ensureLspDefaults(diagnostics: string[]): void {
  const path = join(homedir(), ".pi", "agent", "lsp.json");
  if (existsSync(path)) return;
  try {
    writePrivateJson(path, { installMode: "auto", warmup: true });
  } catch (error) {
    diagnostics.push(`Could not create managed LSP defaults: ${message(error)}`);
  }
}

function ensureChannelMcpDefaults(diagnostics: string[]): void {
  const path = join(homedir(), ".pi", "agent", "mcp.json");
  let config: Record<string, unknown> = {};
  if (existsSync(path)) {
    try {
      const parsed = JSON.parse(readFileSync(path, "utf8")) as unknown;
      if (!isRecord(parsed)) {
        diagnostics.push(`Skipped managed Channel MCP entry because ${path} is not a JSON object`);
        return;
      }
      config = parsed;
    } catch (error) {
      diagnostics.push(`Skipped managed Channel MCP entry because ${path} is invalid JSON: ${message(error)}`);
      return;
    }
  }

  const servers = isRecord(config.mcpServers) ? { ...config.mcpServers } : {};
  const current = servers["agent-society-channel"];
  if (isRecord(current) && !isManagedChannelEntry(current)) return;

  const repositoryRoot = findRepositoryRoot();
  const srcRoot = join(repositoryRoot, "src");
  const httpUrl = (
    process.env.AGENT_CHANNEL_HTTP_URL?.trim() || "http://127.0.0.1:8742"
  ).replace(/\/$/, "");
  const httpToken = process.env.AGENT_CHANNEL_HTTP_TOKEN?.trim() || "";
  const pythonPath = process.env.PYTHONPATH
    ? `${srcRoot}${delimiter}${process.env.PYTHONPATH}`
    : srcRoot;
  let pythonCommand: string;
  try {
    pythonCommand = resolveCompatiblePython();
  } catch (error) {
    diagnostics.push(`Could not configure managed Channel MCP: ${message(error)}`);
    return;
  }
  servers["agent-society-channel"] = {
    command: pythonCommand,
    args: ["-m", "agent_channel.mcp_server"],
    cwd: repositoryRoot,
    env: {
      PYTHONPATH: pythonPath,
      AGENT_CHANNEL_HTTP_URL: httpUrl,
      AGENT_CHANNEL_HTTP_TOKEN: httpToken,
      AGENT_SOCIETY_MANAGED_MCP: MANAGED_CHANNEL_MARKER,
    },
    lifecycle: "eager",
    directTools: true,
    toolPrefix: "none",
  };
  try {
    writePrivateJson(path, {
      ...config,
      mcpServers: servers,
      settings: isRecord(config.settings)
        ? config.settings
        : { outputGuard: true },
    });
  } catch (error) {
    diagnostics.push(`Could not create managed Channel MCP entry: ${message(error)}`);
  }
}

function resolveCompatiblePython(): string {
  const override = process.env.AGENT_PYTHON_COMMAND?.trim();
  if (override) {
    const resolved = probePython(override);
    if (!resolved) {
      throw new Error(
        `AGENT_PYTHON_COMMAND=${override} is not a working Python 3.11+ executable`,
      );
    }
    return resolved;
  }

  const candidates = [
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "python3.14",
    "python3.13",
    "python3.12",
    "python3.11",
    "python3",
    "python",
  ];
  for (const candidate of candidates) {
    const resolved = probePython(candidate);
    if (resolved) return resolved;
  }
  throw new Error(
    "Channel tools require Python 3.11+; install a compatible runtime or set AGENT_PYTHON_COMMAND",
  );
}

function probePython(command: string): string | undefined {
  const result = spawnSync(
    command,
    [
      "-c",
      "import os, sys; from enum import StrEnum; print(os.path.realpath(sys.executable))",
    ],
    {
      encoding: "utf8",
      timeout: 5_000,
      windowsHide: true,
    },
  );
  if (result.status !== 0) return undefined;
  const executable = result.stdout.trim();
  return executable || undefined;
}

function isManagedChannelEntry(value: Record<string, unknown>): boolean {
  if (!isRecord(value.env)) return false;
  return value.env.AGENT_SOCIETY_MANAGED_MCP === MANAGED_CHANNEL_MARKER;
}

function findRepositoryRoot(): string {
  let cursor = dirname(fileURLToPath(import.meta.url));
  for (let depth = 0; depth < 8; depth += 1) {
    if (
      existsSync(join(cursor, "pyproject.toml")) &&
      existsSync(join(cursor, "agent-host", "package.json"))
    ) {
      return cursor;
    }
    const parent = dirname(cursor);
    if (parent === cursor) break;
    cursor = parent;
  }
  throw new Error("Could not locate the AgentSociety repository root");
}

function writePrivateJson(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${process.pid}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  renameSync(temporary, path);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
