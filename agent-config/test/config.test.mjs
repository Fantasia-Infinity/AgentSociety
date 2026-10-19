import assert from "node:assert/strict";
import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  canonicalConfigPath,
  describeConfig,
  loadAgentConfig,
  parseEnv,
  resolveCredential,
  writeAgentConfig,
  writeCredential,
} from "../lib/index.js";

test("configuration precedence is legacy, dsh, explicit, process", async () => {
  const dir = await mkdtemp(join(tmpdir(), "agent-config-"));
  const dshHome = join(dir, "dsh");
  const legacy = join(dir, "legacy.env");
  const explicit = join(dir, "explicit.env");
  await writeFile(legacy, "AGENT_WORKSPACE_ROOT=/legacy\nAGENT_DSH_MODEL=legacy\n");
  await writeAgentConfig({ AGENT_DSH_MODEL: "dsh" }, { dshHome });
  await writeFile(explicit, "AGENT_DSH_MODEL=explicit\n");
  const config = loadAgentConfig({
    dshHome,
    legacyPaths: [legacy],
    configPath: explicit,
    env: { DSH_HOME: dshHome, AGENT_DSH_MODEL: "process" },
  });
  assert.equal(config.values.AGENT_DSH_MODEL, "process");
  assert.equal(config.values.AGENT_WORKSPACE_ROOT, "/legacy");
  assert.equal(config.source, "explicit");
});

test("writeAgentConfig excludes secret values and file credentials are 0600", async () => {
  const dir = await mkdtemp(join(tmpdir(), "agent-config-"));
  const dshHome = join(dir, "dsh");
  const configPath = join(dshHome, ".env");
  writeAgentConfig(
    {
      AGENT_WORKSPACE_ROOT: "/tmp/workspace",
      AGENT_REMOTE_API_KEY: "must-not-be-written",
      AGENT_CREDENTIAL_BACKEND: "file",
      AGENT_CREDENTIAL_FILE: join(dir, "credentials.env"),
    },
    { path: configPath, dshHome },
  );
  const text = await readFile(configPath, "utf8");
  assert.match(text, /AGENT_WORKSPACE_ROOT/);
  assert.doesNotMatch(text, /must-not-be-written/);
  const written = writeCredential("AGENT_REMOTE_API_KEY", "secret", {
    backend: "file",
    filePath: join(dir, "credentials.env"),
    dshHome,
  });
  assert.equal(written.backend, "file");
  assert.equal((await stat(join(dir, "credentials.env"))).mode & 0o777, 0o600);
  assert.equal(
    resolveCredential("AGENT_REMOTE_API_KEY", {
      backend: "file",
      filePath: join(dir, "credentials.env"),
      dshHome,
    }).value,
    "secret",
  );
  assert.equal(
    describeConfig(loadAgentConfig({ dshHome })).credentialBackend,
    "file",
  );
  assert.equal(canonicalConfigPath(dshHome), configPath);
});

test("parseEnv handles quoted values", () => {
  assert.deepEqual(parseEnv('A="b c"\n# comment\n'), { A: "b c" });
});
