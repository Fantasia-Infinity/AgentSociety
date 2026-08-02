import { stdin, stdout } from "node:process";
import { createInterface } from "node:readline/promises";

import {
  createAgentSessionServices,
  hasTrustRequiringProjectResources,
  ProjectTrustStore,
  SettingsManager,
  type AgentSession,
  type AgentSessionRuntimeDiagnostic,
  type AgentSessionServices,
  type LoadExtensionsResult,
  type ModelRuntime,
  type ProjectTrustContext,
  type ProjectTrustEventResult,
} from "@earendil-works/pi-coding-agent";

import type {
  RemotePiResourcePolicy,
  RemoteToolPolicy,
} from "./config.js";

export type PiConversationMode = "local" | "remote" | "diagnostic";

interface CreatePiServicesOptions {
  cwd: string;
  agentDir: string;
  modelRuntime: ModelRuntime;
  mode: PiConversationMode;
  remotePiResourcePolicy: RemotePiResourcePolicy;
  projectTrustContext?: ProjectTrustContext;
}

export async function createPiServices(
  options: CreatePiServicesOptions,
): Promise<AgentSessionServices> {
  const trustDiagnostics: AgentSessionRuntimeDiagnostic[] = [];
  const settingsManager = createSettingsManager(options);
  const resourcesDisabled =
    options.remotePiResourcePolicy === "disabled" &&
    options.mode !== "local";
  const services = await createAgentSessionServices({
    cwd: options.cwd,
    agentDir: options.agentDir,
    modelRuntime: options.modelRuntime,
    settingsManager,
    ...(resourcesDisabled
      ? {
          resourceLoaderOptions: {
            noExtensions: true,
            noSkills: true,
            noPromptTemplates: true,
            noThemes: true,
          },
        }
      : {}),
    ...(options.mode === "local"
      ? {
          resourceLoaderReloadOptions: {
            resolveProjectTrust: async ({ extensionsResult }) =>
              resolveLocalProjectTrust(
                options,
                settingsManager,
                extensionsResult,
                trustDiagnostics,
              ),
          },
        }
      : {}),
  });
  services.diagnostics.push(...trustDiagnostics);
  return services;
}

function createSettingsManager(
  options: CreatePiServicesOptions,
): SettingsManager {
  if (options.mode === "local") {
    return SettingsManager.create(options.cwd, options.agentDir, {
      projectTrusted: false,
    });
  }
  if (options.remotePiResourcePolicy === "disabled") {
    return SettingsManager.inMemory({}, { projectTrusted: false });
  }
  const settingsManager = SettingsManager.create(
    options.cwd,
    options.agentDir,
    { projectTrusted: false },
  );
  if (options.remotePiResourcePolicy === "trusted_project") {
    const trustStore = new ProjectTrustStore(options.agentDir);
    const stored = trustStore.get(options.cwd);
    const defaultTrust = settingsManager.getDefaultProjectTrust();
    settingsManager.setProjectTrusted(
      stored === true || (stored === null && defaultTrust === "always"),
    );
  }
  return settingsManager;
}

async function resolveLocalProjectTrust(
  options: CreatePiServicesOptions,
  settingsManager: SettingsManager,
  extensionsResult: LoadExtensionsResult,
  diagnostics: AgentSessionRuntimeDiagnostic[],
): Promise<boolean> {
  if (!hasTrustRequiringProjectResources(options.cwd)) return true;
  const trustStore = new ProjectTrustStore(options.agentDir);
  const stored = trustStore.get(options.cwd);
  if (stored !== null) return stored;
  const defaultTrust = settingsManager.getDefaultProjectTrust();
  if (defaultTrust === "always") return true;
  if (defaultTrust === "never") return false;

  const context =
    options.projectTrustContext ?? createTerminalTrustContext(options.cwd);
  const extensionDecision = await askTrustExtensions(
    extensionsResult,
    options.cwd,
    context,
    diagnostics,
  );
  if (extensionDecision) {
    const trusted = extensionDecision.trusted === "yes";
    if (extensionDecision.remember) trustStore.set(options.cwd, trusted);
    return trusted;
  }

  const choice = await context.ui.select(
    `Trust project-local Pi resources?\n${options.cwd}\n\nTrusted projects may execute .pi extensions and install project packages.`,
    [
      "Trust",
      "Trust (this session only)",
      "Do not trust",
      "Do not trust (this session only)",
    ],
  );
  if (choice === "Trust") {
    trustStore.set(options.cwd, true);
    return true;
  }
  if (choice === "Trust (this session only)") return true;
  if (choice === "Do not trust") trustStore.set(options.cwd, false);
  return false;
}

async function askTrustExtensions(
  extensionsResult: LoadExtensionsResult,
  cwd: string,
  context: ProjectTrustContext,
  diagnostics: AgentSessionRuntimeDiagnostic[],
): Promise<ProjectTrustEventResult | undefined> {
  type TrustHandler = (
    event: { type: "project_trust"; cwd: string },
    ctx: ProjectTrustContext,
  ) => Promise<ProjectTrustEventResult> | ProjectTrustEventResult;
  for (const extension of extensionsResult.extensions) {
    const handlers = extension.handlers.get("project_trust") ?? [];
    for (const handler of handlers) {
      try {
        const result = await (handler as TrustHandler)(
          { type: "project_trust", cwd },
          context,
        );
        if (result?.trusted === "yes" || result?.trusted === "no") {
          return result;
        }
      } catch (error) {
        diagnostics.push({
          type: "warning",
          message: `Extension "${extension.path}" project_trust error: ${errorMessage(error)}`,
        });
      }
    }
  }
  return undefined;
}

function createTerminalTrustContext(cwd: string): ProjectTrustContext {
  return {
    cwd,
    mode: "tui",
    hasUI: Boolean(stdin.isTTY && stdout.isTTY),
    ui: {
      select: async (title, options) => {
        if (!stdin.isTTY || !stdout.isTTY) return undefined;
        stdout.write(`\n${title}\n`);
        options.forEach((option, index) => {
          stdout.write(`  ${index + 1}. ${option}\n`);
        });
        const answer = await terminalQuestion("Select [4]: ");
        const index = Number(answer || options.length) - 1;
        return options[index];
      },
      confirm: async (title, message) => {
        if (!stdin.isTTY || !stdout.isTTY) return false;
        const answer = await terminalQuestion(`${title}\n${message} [y/N]: `);
        return /^(?:y|yes)$/iu.test(answer);
      },
      input: async (title, placeholder) => {
        if (!stdin.isTTY || !stdout.isTTY) return undefined;
        return terminalQuestion(`${title}${placeholder ? ` [${placeholder}]` : ""}: `);
      },
      notify: (message) => {
        stdout.write(`${message}\n`);
      },
    },
  };
}

async function terminalQuestion(prompt: string): Promise<string> {
  const terminal = createInterface({ input: stdin, output: stdout });
  try {
    return (await terminal.question(prompt)).trim();
  } finally {
    terminal.close();
  }
}

export function collectPiDiagnostics(
  services: AgentSessionServices,
): AgentSessionRuntimeDiagnostic[] {
  const diagnostics = [...services.diagnostics];
  for (const item of services.resourceLoader.getExtensions().errors) {
    diagnostics.push({
      type: "error",
      message: `Failed to load extension "${item.path}": ${item.error}`,
    });
  }
  for (const [kind, result] of [
    ["skill", services.resourceLoader.getSkills()],
    ["prompt", services.resourceLoader.getPrompts()],
    ["theme", services.resourceLoader.getThemes()],
  ] as const) {
    for (const item of result.diagnostics) {
      diagnostics.push({
        type: item.type === "collision" ? "warning" : item.type,
        message: `${kind} resource${item.path ? ` "${item.path}"` : ""}: ${item.message}`,
      });
    }
  }
  return diagnostics;
}

export function sessionToolSelection(
  mode: PiConversationMode,
  remoteToolPolicy: RemoteToolPolicy,
  restrictedToolNames: string[],
): string[] | undefined {
  if (mode === "local") return undefined;
  if (mode === "remote" && remoteToolPolicy === "full") return undefined;
  return restrictedToolNames;
}

export function activateCompatibleTools(
  session: AgentSession,
  mode: PiConversationMode,
  remoteToolPolicy: RemoteToolPolicy,
): void {
  if (mode === "local" || (mode === "remote" && remoteToolPolicy === "full")) {
    session.setActiveToolsByName(
      session.getAllTools().map((tool) => tool.name),
    );
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
