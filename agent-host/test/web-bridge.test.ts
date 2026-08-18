import assert from "node:assert/strict";
import { test } from "node:test";

import { assertLoopbackTarget } from "../src/web-bridge.js";

test("web-bridge accepts loopback targets only", () => {
  assert.equal(assertLoopbackTarget("http://127.0.0.1:3001"), "http://127.0.0.1:3001");
  assert.equal(assertLoopbackTarget("http://localhost:3001"), "http://localhost:3001");
  assert.equal(assertLoopbackTarget("http://127.0.0.1:3001/"), "http://127.0.0.1:3001");
  assert.throws(() => assertLoopbackTarget("http://192.168.1.10:3001"));
  assert.throws(() => assertLoopbackTarget("http://example.com:3001"));
  assert.throws(() => assertLoopbackTarget("http://[::ffff:1.2.3.4]:3001"));
});
