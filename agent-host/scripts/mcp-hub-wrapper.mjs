#!/usr/bin/env node
// Secure Hub MCP bridge for stdio-based MCP clients (Codex, OpenCode, ...).
//
// Reads the per-node Hub credential from the system keychain and proxies
// stdio JSON-RPC to the Hub's Streamable HTTP MCP endpoint. The token never
// lives in config files or on disk.
//
// The Hub answers every MCP message directly in the POST response, so this
// bridge is a plain request/response proxy and does not depend on
// mcp-remote's OAuth-first behavior.
//
// Configuration resolution (first non-empty wins):
//   url:      AGENT_HUB_MCP_URL | AGENT_HUB_URL from .private/env/agent.env (or .env.agent) + "/mcp"
//   service:  AGENT_HUB_MCP_KEYCHAIN_SERVICE | AGENT_HUB_NODE_TOKEN_CREDENTIAL_SERVICE from env file | "AgentSociety Hub Node"
//   account:  AGENT_HUB_MCP_KEYCHAIN_ACCOUNT | AGENT_HUB_NODE_TOKEN_CREDENTIAL_ACCOUNT from env file | "" (any account)
//
// Environment overrides:
//   AGENT_HUB_MCP_URL              Hub MCP endpoint (defaults to AGENT_HUB_URL + /mcp)
//   AGENT_HUB_MCP_KEYCHAIN_SERVICE keychain service override
//   AGENT_HUB_MCP_KEYCHAIN_ACCOUNT keychain account override
import { execFileSync } from "node:child_process";
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

process.stdin.setEncoding("utf8");
let buffer = "";
for await (const chunk of process.stdin) {
  buffer += chunk;
  let newline;
  while ((newline = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, newline).trim();
    buffer = buffer.slice(newline + 1);
    if (!line) continue;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      continue;
    }
    const hasId =
      message && typeof message === "object" && message.id !== undefined && message.id !== null;
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          Accept: "application/json, text/event-stream",
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: line,
      });
      if (!hasId) continue;
      const text = (await response.text()).trim();
      if (!text) continue;
      if (response.ok) {
        process.stdout.write(text + "\n");
      } else {
        process.stdout.write(
          JSON.stringify({
            jsonrpc: "2.0",
            id: message.id,
            error: {
              code: -32603,
              message: `Hub MCP returned HTTP ${response.status}: ${text.slice(0, 500)}`,
            },
          }) + "\n",
        );
      }
    } catch (error) {
      if (!hasId) continue;
      process.stdout.write(
        JSON.stringify({
          jsonrpc: "2.0",
          id: message.id,
          error: {
            code: -32603,
            message: `Hub MCP request failed: ${error instanceof Error ? error.message : String(error)}`,
          },
        }) + "\n",
      );
    }
  }
}
