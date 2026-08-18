// Cross-stack smoke driver: compiled TS WebBridge against a live Python Hub.
import { writeFileSync, rmSync } from "node:fs";
import { WebBridge } from "../agent-host/dist/src/web-bridge.js";

const marker = "/tmp/dsh-bridge-smoke-ready";
rmSync(marker, { force: true });

const hubUrl = process.env.SMOKE_HUB;
const target = process.env.SMOKE_TARGET;
const nodeToken = process.env.SMOKE_NODE_TOKEN;
const nodeId = process.env.SMOKE_NODE_ID;
if (!hubUrl || !target || !nodeToken || !nodeId) {
  console.error("missing SMOKE_* env");
  process.exit(2);
}

const bridge = new WebBridge({ hubUrl, nodeToken, nodeId, target });
const stop = () => bridge.stop();
process.once("SIGINT", stop);
process.once("SIGTERM", stop);

bridge.run().catch((error) => {
  console.error("bridge failed:", error.message);
  process.exit(1);
});

// Wait until the Python driver signals a tunnel is open (first successful
// connection), then keep the process alive briefly for the proxy roundtrip.
setTimeout(() => {
  writeFileSync(marker, "ready");
  setTimeout(() => {
    stop();
    process.exit(0);
  }, 60_000);
}, 2_000);
