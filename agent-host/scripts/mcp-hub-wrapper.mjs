import { execFileSync } from "node:child_process";
import { appendFileSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
const LOG = "/tmp/opencode-mcp-debug2.log";
const log = (s) => appendFileSync(LOG, `${new Date().toISOString()} ${s}\n`);
const projectEnv = {};
try {
  for (const raw of readFileSync(fileURLToPath(new URL("../../.private/env/agent.env", import.meta.url)), "utf8").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    projectEnv[key] = value;
  }
} catch {}
const url = (projectEnv.AGENT_HUB_URL || "").replace(/\/+$/, "") + "/mcp";
const token = execFileSync("/usr/bin/security", ["find-generic-password", "-s", "AgentSociety Hub Node", "-a", "shufanzhang", "-w"], { encoding: "utf8" }).trim();
log(`started url=${url}`);
process.stdin.setEncoding("utf8");
let buffer = "";
process.stdin.on("data", async (chunk) => {
  buffer += chunk;
  let idx;
  while ((idx = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    let message;
    try { message = JSON.parse(line); } catch { continue; }
    log("GOT: " + line.slice(0, 200));
    const hasId = message && typeof message === "object" && message.id !== undefined && message.id !== null;
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { Accept: "application/json, text/event-stream", "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: line,
      });
      const text = (await response.text()).trim();
      log(`HTTP ${response.status}: ${text.slice(0, 200)}`);
      if (hasId && text && response.ok) {
        process.stdout.write(text + "\n");
      }
    } catch (error) {
      log(`FETCH-FAIL: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
});
