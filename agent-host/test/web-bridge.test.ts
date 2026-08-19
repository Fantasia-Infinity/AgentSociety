import assert from "node:assert/strict";
import { test } from "node:test";

import { assertLoopbackTarget, buildLocalUrl } from "../src/web-bridge.js";

test("web-bridge accepts loopback targets only", () => {
  assert.equal(
    assertLoopbackTarget("http://127.0.0.1:3001"),
    "http://127.0.0.1:3001",
  );
  assert.equal(
    assertLoopbackTarget("http://localhost:3001"),
    "http://localhost:3001",
  );
  assert.equal(
    assertLoopbackTarget("http://127.0.0.1:3001/"),
    "http://127.0.0.1:3001",
  );
  assert.throws(() => assertLoopbackTarget("http://192.168.1.10:3001"));
  assert.throws(() => assertLoopbackTarget("http://127.example.com:3001"));
  assert.throws(() => assertLoopbackTarget("http://[::ffff:1.2.3.4]:3001"));
});

test("buildLocalUrl only permits origin-local dsh web paths", () => {
  assert.equal(
    buildLocalUrl("http://127.0.0.1:3080", "/api/session.list?x=1").href,
    "http://127.0.0.1:3080/api/session.list?x=1",
  );
  assert.equal(
    buildLocalUrl("http://127.0.0.1:3080", "/assets/app.js").pathname,
    "/assets/app.js",
  );
  assert.equal(buildLocalUrl("http://127.0.0.1:3080", "/").pathname, "/");
  assert.throws(() =>
    buildLocalUrl("http://127.0.0.1:3080", "/api/../assets/app.js"),
  );
  assert.throws(() =>
    buildLocalUrl("http://127.0.0.1:3080", "/api/%2e%2e/assets/app.js"),
  );
  assert.throws(() =>
    buildLocalUrl("http://127.0.0.1:3080", "/api/%252e%252e/assets/app.js"),
  );
  assert.throws(() =>
    buildLocalUrl("http://127.0.0.1:3080", "/api/./session.list"),
  );
  assert.throws(() => buildLocalUrl("http://127.0.0.1:3080", "/etc/passwd"));
  assert.throws(() =>
    buildLocalUrl("http://127.0.0.1:3080", "//example.com/api"),
  );
  assert.throws(() =>
    buildLocalUrl("http://127.0.0.1:3080", "/api#fragment"),
  );
});
