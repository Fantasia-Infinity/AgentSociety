#!/usr/bin/env node
// Secure Hub MCP bridge for Codex (macOS).
//
// Reads the per-node Hub credential from the system keychain and proxies
// stdio to mcp-remote. The token never lives in config.toml or on disk.
//
// Environment overrides:
//   AGENT_HUB_MCP_URL              required (e.g. https://hub.example.com/mcp)
//   AGENT_HUB_MCP_REMOTE_BIN       default mcp-remote (resolved from PATH)
//   AGENT_HUB_MCP_KEYCHAIN_SERVICE default AgentSociety Hub Node
//   AGENT_HUB_MCP_KEYCHAIN_ACCOUNT default (omit -> any account)
import { execFileSync, spawn } from "node:child_process";

const service =
  process.env.AGENT_HUB_MCP_KEYCHAIN_SERVICE || "AgentSociety Hub Node";
const account = process.env.AGENT_HUB_MCP_KEYCHAIN_ACCOUNT || "";
const url = process.env.AGENT_HUB_MCP_URL?.trim() || "";
const mcpRemote =
  process.env.AGENT_HUB_MCP_REMOTE_BIN?.trim() || "mcp-remote";

if (!url) {
  console.error(
    "AGENT_HUB_MCP_URL is required (e.g. https://hub.example.com/mcp); set it before starting Codex.",
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
  [url, "--header", "Authorization: Bearer ${AGENT_HUB_MCP_TOKEN}"],
  {
    stdio: "inherit",
    env: { ...process.env, AGENT_HUB_MCP_TOKEN: token },
  },
);
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 0);
});
