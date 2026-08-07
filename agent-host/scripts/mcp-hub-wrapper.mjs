#!/usr/bin/env node
// Secure Hub MCP bridge for Codex (macOS).
//
// Reads the per-node Hub credential from the system keychain and proxies
// stdio to mcp-remote. The token never lives in config.toml or on disk.
//
// Configuration resolution (first non-empty wins):
//   url:      AGENT_HUB_MCP_URL | AGENT_HUB_URL from .private/env/agent.env (or .env.agent) + "/mcp"
//   service:  AGENT_HUB_MCP_KEYCHAIN_SERVICE | AGENT_HUB_NODE_TOKEN_CREDENTIAL_SERVICE from env file | "AgentSociety Hub Node"
//   account:  AGENT_HUB_MCP_KEYCHAIN_ACCOUNT | AGENT_HUB_NODE_TOKEN_CREDENTIAL_ACCOUNT from env file | "" (any account)
//
// Environment overrides:
//   AGENT_HUB_MCP_URL              Hub MCP endpoint (defaults to AGENT_HUB_URL + /mcp)
//   AGENT_HUB_MCP_REMOTE_BIN       default mcp-remote (resolved from PATH)
//   AGENT_HUB_MCP_KEYCHAIN_SERVICE keychain service override
//   AGENT_HUB_MCP_KEYCHAIN_ACCOUNT keychain account override
import { execFileSync, spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

function loadProjectEnv() {
  const candidates = [
    fileURLToPath(new URL("../../.private/env/agent.env", import.meta.url)),
    fileURLToPath(new URL("../../.env.agent", import.meta.url)),
  ];
  const values = {};
  for (const path of candidates) {
    try {
      for (const raw of readFileSync(path, "utf8").split(/\r?\n/)) {
        const line = raw.trim();
        if (!line || line.startsWith("#")) continue;
        const eq = line.indexOf("=");
        if (eq <= 0) continue;
        const key = line.slice(0, eq).trim();
        let value = line.slice(eq + 1).trim();
        if (
          (value.startsWith('"') && value.endsWith('"')) ||
          (value.startsWith("'") && value.endsWith("'"))
        ) {
          value = value.slice(1, -1);
        }
        values[key] = value;
      }
    } catch {
      // try the next candidate
    }
    if (Object.keys(values).length) break;
  }
  return values;
}

const projectEnv = loadProjectEnv();

const service =
  process.env.AGENT_HUB_MCP_KEYCHAIN_SERVICE ||
  projectEnv.AGENT_HUB_NODE_TOKEN_CREDENTIAL_SERVICE ||
  "AgentSociety Hub Node";
const account =
  process.env.AGENT_HUB_MCP_KEYCHAIN_ACCOUNT ||
  projectEnv.AGENT_HUB_NODE_TOKEN_CREDENTIAL_ACCOUNT ||
  "";
const hubUrl = (
  process.env.AGENT_HUB_MCP_URL ||
  projectEnv.AGENT_HUB_URL ||
  ""
).trim();
const url = hubUrl.endsWith("/mcp")
  ? hubUrl
  : `${hubUrl.replace(/\/+$/, "")}/mcp`;
const mcpRemote =
  process.env.AGENT_HUB_MCP_REMOTE_BIN?.trim() || "mcp-remote";

if (!hubUrl) {
  console.error(
    "AGENT_HUB_MCP_URL is required (or AGENT_HUB_URL in .private/env/agent.env); " +
      "run `agent connect` first.",
  );
  process.exit(1);
}

let token = "";
try {
  const args = ["find-generic-password", "-s", service, "-w"];
  if (account) args.push("-a", account);
  token = execFileSync("/usr/bin/security", args, { encoding: "utf8" }).trim();
} catch {
  console.error(
    "Hub node credential not found in the keychain; run `agent connect` first.",
  );
  process.exit(1);
}
if (!token) {
  console.error("Hub node credential is empty.");
  process.exit(1);
}

const child = spawn(
  mcpRemote,
  [url, "--header", `Authorization: Bearer ${token}`],
  {
    stdio: "inherit",
  },
);
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 0);
});
