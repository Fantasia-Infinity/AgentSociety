import assert from "node:assert/strict";
import { test } from "node:test";

import { dshEncodeSegment, dshProjectKey, dshSessionLogPath } from "../src/dsh-paths.js";

test("dsh path encoding matches the upstream runtime layout", () => {
  assert.equal(dshEncodeSegment(".."), "~002E~002E");
  assert.equal(dshEncodeSegment("a/b"), "a~002Fb");
  assert.equal(dshProjectKey("/tmp/workspace"), "--tmp-workspace--");
  assert.equal(
    dshSessionLogPath("/root/sessions", "/tmp/workspace", "dsh-abc", "none"),
    "/root/sessions/--tmp-workspace--/dsh-abc/session.jsonl",
  );
  assert.equal(
    dshSessionLogPath("/root/sessions", "/tmp/workspace", "dsh-abc", "zstd"),
    "/root/sessions/--tmp-workspace--/dsh-abc/session.jsonl.zstd",
  );
});
