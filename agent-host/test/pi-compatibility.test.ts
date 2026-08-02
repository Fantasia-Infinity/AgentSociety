import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import {
  createAgentSessionFromServices,
  ModelRuntime,
  ProjectTrustStore,
  SessionManager,
} from "@earendil-works/pi-coding-agent";

import {
  activateCompatibleTools,
  createPiServices,
  sessionToolSelection,
} from "../src/pi-compatibility.js";

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const path of temporaryDirectories.splice(0)) {
    rmSync(path, { recursive: true, force: true });
  }
});

function temporaryDirectory(): string {
  const path = mkdtempSync(join(tmpdir(), "pi-compatibility-test-"));
  temporaryDirectories.push(path);
  return path;
}

function writeExtension(
  directory: string,
  fileName: string,
  toolName: string,
  commandName: string,
): void {
  mkdirSync(directory, { recursive: true });
  writeFileSync(
    join(directory, fileName),
    [
      'import { defineTool } from "@mariozechner/pi-coding-agent";',
      'import { Type } from "typebox";',
      "export default function (pi) {",
      "  pi.registerTool(defineTool({",
      `    name: ${JSON.stringify(toolName)},`,
      `    label: ${JSON.stringify(toolName)},`,
      '    description: "Pi compatibility fixture",',
      "    parameters: Type.Object({}),",
      '    execute: async () => ({ content: [{ type: "text", text: "ok" }], details: {} }),',
      "  }));",
      `  pi.registerCommand(${JSON.stringify(commandName)}, {`,
      '    description: "Pi compatibility fixture command",',
      "    handler: async () => {},",
      "  });",
      "}",
      "",
    ].join("\n"),
  );
}

function writeSkill(agentDir: string): void {
  const directory = join(agentDir, "skills", "community-skill");
  mkdirSync(directory, { recursive: true });
  writeFileSync(
    join(directory, "SKILL.md"),
    [
      "---",
      "name: community-skill",
      "description: Pi compatibility fixture skill",
      "---",
      "Use the community compatibility fixture.",
      "",
    ].join("\n"),
  );
}

function configureLocalPackage(agentDir: string, packageDir: string): void {
  mkdirSync(agentDir, { recursive: true });
  writeFileSync(
    join(packageDir, "package.json"),
    JSON.stringify(
      {
        name: "community-package-fixture",
        version: "1.0.0",
        pi: {
          extensions: ["./extensions"],
          skills: ["./skills"],
        },
      },
      null,
      2,
    ),
  );
  writeFileSync(
    join(agentDir, "settings.json"),
    JSON.stringify({ packages: [packageDir] }, null, 2),
  );
}

async function modelRuntime() {
  const runtime = await ModelRuntime.create();
  runtime.registerProvider("fixture", {
    name: "Fixture",
    baseUrl: "https://models.test.invalid/v1",
    apiKey: "fixture-key",
    api: "openai-completions",
    models: [
      {
        id: "fixture-model",
        name: "Fixture model",
        api: "openai-completions",
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 8_192,
        maxTokens: 1_024,
      },
    ],
  });
  return runtime;
}

test("local Pi sessions load community tools, commands, and skills", async () => {
  const root = temporaryDirectory();
  const agentDir = join(root, "agent");
  const workspace = join(root, "workspace");
  const packageDir = join(root, "community-package");
  mkdirSync(workspace, { recursive: true });
  writeExtension(
    join(packageDir, "extensions"),
    "global-extension.ts",
    "community_global_tool",
    "community-global",
  );
  writeExtension(
    join(workspace, ".pi", "extensions"),
    "project-extension.ts",
    "community_project_tool",
    "community-project",
  );
  writeSkill(packageDir);
  configureLocalPackage(agentDir, packageDir);
  new ProjectTrustStore(agentDir).set(workspace, true);

  const runtime = await modelRuntime();
  const services = await createPiServices({
    cwd: workspace,
    agentDir,
    modelRuntime: runtime,
    mode: "local",
    remotePiResourcePolicy: "disabled",
  });
  const extensions = services.resourceLoader.getExtensions().extensions;
  assert.deepEqual(
    new Set(extensions.flatMap((extension) => [...extension.tools.keys()])),
    new Set(["community_global_tool", "community_project_tool"]),
  );
  assert.deepEqual(
    new Set(extensions.flatMap((extension) => [...extension.commands.keys()])),
    new Set(["community-global", "community-project"]),
  );
  assert.ok(
    services.resourceLoader
      .getSkills()
      .skills.some((skill) => skill.name === "community-skill"),
  );

  const model = runtime.getModel("fixture", "fixture-model");
  assert.ok(model);
  const { session } = await createAgentSessionFromServices({
    services,
    model,
    sessionManager: SessionManager.inMemory(workspace),
  });
  try {
    activateCompatibleTools(session, "local", "read_only");
    assert.ok(session.getActiveToolNames().includes("community_global_tool"));
    assert.ok(session.getActiveToolNames().includes("community_project_tool"));
    assert.ok(session.getActiveToolNames().includes("grep"));
  } finally {
    session.dispose();
  }
});

test("remote Pi resources require an explicit policy", async () => {
  const root = temporaryDirectory();
  const agentDir = join(root, "agent");
  const workspace = join(root, "workspace");
  mkdirSync(workspace, { recursive: true });
  writeExtension(
    join(agentDir, "extensions"),
    "global-extension.ts",
    "community_global_tool",
    "community-global",
  );
  writeExtension(
    join(workspace, ".pi", "extensions"),
    "project-extension.ts",
    "community_project_tool",
    "community-project",
  );
  const runtime = await modelRuntime();

  const disabled = await createPiServices({
    cwd: workspace,
    agentDir,
    modelRuntime: runtime,
    mode: "remote",
    remotePiResourcePolicy: "disabled",
  });
  assert.equal(disabled.resourceLoader.getExtensions().extensions.length, 0);

  const global = await createPiServices({
    cwd: workspace,
    agentDir,
    modelRuntime: runtime,
    mode: "remote",
    remotePiResourcePolicy: "global",
  });
  assert.deepEqual(
    global.resourceLoader
      .getExtensions()
      .extensions.flatMap((extension) => [...extension.tools.keys()]),
    ["community_global_tool"],
  );

  const model = runtime.getModel("fixture", "fixture-model");
  assert.ok(model);
  const readOnlyTools = sessionToolSelection(
    "remote",
    "read_only",
    ["read", "grep"],
  );
  const { session: readOnlySession } = await createAgentSessionFromServices({
    services: global,
    model,
    sessionManager: SessionManager.inMemory(workspace),
    ...(readOnlyTools ? { tools: readOnlyTools } : {}),
  });
  try {
    assert.deepEqual(readOnlySession.getActiveToolNames().sort(), ["grep", "read"]);
    assert.ok(
      !readOnlySession
        .getAllTools()
        .some((tool) => tool.name === "community_global_tool"),
    );
  } finally {
    readOnlySession.dispose();
  }

  const fullServices = await createPiServices({
    cwd: workspace,
    agentDir,
    modelRuntime: runtime,
    mode: "remote",
    remotePiResourcePolicy: "global",
  });
  const { session: fullSession } = await createAgentSessionFromServices({
    services: fullServices,
    model,
    sessionManager: SessionManager.inMemory(workspace),
  });
  try {
    activateCompatibleTools(fullSession, "remote", "full");
    assert.ok(fullSession.getActiveToolNames().includes("community_global_tool"));
  } finally {
    fullSession.dispose();
  }

  new ProjectTrustStore(agentDir).set(workspace, true);
  const trustedProject = await createPiServices({
    cwd: workspace,
    agentDir,
    modelRuntime: runtime,
    mode: "remote",
    remotePiResourcePolicy: "trusted_project",
  });
  assert.deepEqual(
    new Set(
      trustedProject.resourceLoader
        .getExtensions()
        .extensions.flatMap((extension) => [...extension.tools.keys()]),
    ),
    new Set(["community_global_tool", "community_project_tool"]),
  );
});

test("local project resources stay disabled without a trust decision", async () => {
  const root = temporaryDirectory();
  const agentDir = join(root, "agent");
  const workspace = join(root, "workspace");
  mkdirSync(workspace, { recursive: true });
  writeExtension(
    join(agentDir, "extensions"),
    "global-extension.ts",
    "community_global_tool",
    "community-global",
  );
  writeExtension(
    join(workspace, ".pi", "extensions"),
    "project-extension.ts",
    "community_project_tool",
    "community-project",
  );
  const runtime = await modelRuntime();
  const services = await createPiServices({
    cwd: workspace,
    agentDir,
    modelRuntime: runtime,
    mode: "local",
    remotePiResourcePolicy: "disabled",
    projectTrustContext: {
      cwd: workspace,
      mode: "tui",
      hasUI: false,
      ui: {
        select: async () => undefined,
        confirm: async () => false,
        input: async () => undefined,
        notify: () => {},
      },
    },
  });
  assert.deepEqual(
    services.resourceLoader
      .getExtensions()
      .extensions.flatMap((extension) => [...extension.tools.keys()]),
    ["community_global_tool"],
  );
});

test("remote read-only policy keeps community tools outside the registry", () => {
  assert.deepEqual(
    sessionToolSelection("remote", "read_only", ["read", "grep"]),
    ["read", "grep"],
  );
  assert.equal(
    sessionToolSelection("remote", "full", ["read", "bash"]),
    undefined,
  );
  assert.equal(
    sessionToolSelection("local", "read_only", ["read"]),
    undefined,
  );
});
