import { loadAgentConfig, resolveCredential, } from "@agent-society/agent-config";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { recordAgentSocietyMetrics } from "./context-metrics.js";
export const name = "agent-society-config";
export const inject = [];
const NON_SECRET_KEYS = [
    "AGENT_HUB_URL",
    "AGENT_HUB_USERNAME",
    "AGENT_HUB_RECEIVE_DISABLED",
    "AGENT_HUB_DSH_WEB",
    "AGENT_CREDENTIAL_BACKEND",
    "AGENT_CREDENTIAL_FILE",
    "AGENT_PRINCIPAL_ID",
    "AGENT_PRINCIPAL_NAME",
    "AGENT_ACTOR_ID",
    "AGENT_ACTOR_NAME",
    "AGENT_NODE_ID",
    "AGENT_NODE_NAME",
    "AGENT_WORKSPACE_ROOT",
    "AGENT_REMOTE_BASE_URL",
    "AGENT_REMOTE_TOOL_POLICY",
    "AGENT_DSH_PERMISSION_MODE",
    "AGENT_DSH_MODEL",
    "AGENT_DSH_PROVIDER",
    "AGENT_REMOTE_MODEL",
    "AGENT_SOCIETY_PROVIDER",
    "AGENT_SOCIETY_MODEL",
    "AGENT_SOCIETY_HUB_MCP",
    "AGENT_SOCIETY_TOOL_POLICY",
    "AGENT_SOCIETY_WEB_PROFILE",
    "AGENT_SOCIETY_DIRECTORY",
    "AGENT_SOCIETY_CONTEXT_MODE",
    "AGENT_SOCIETY_QUESTIONS",
    "AGENT_SOCIETY_CONTEXT",
    "AGENT_SOCIETY_SESSION_MODE",
    "AGENT_SOCIETY_POLL_SECONDS",
    "AGENT_SOCIETY_LEASE_SECONDS",
];
const CREDENTIAL_KEYS = [
    "AGENT_REMOTE_API_KEY",
    "AGENT_HUB_TOKEN",
    "AGENT_HUB_PASSWORD",
    "AGENT_HUB_NODE_TOKEN",
];
export function apply(ctx) {
    const dshHome = resolve(process.env.DSH_HOME?.trim() || join(homedir(), ".dsh"));
    ctx.on("llm/stream", (_options, next) => observeUsage(dshHome, next));
    const config = loadAgentConfig({ cwd: process.cwd() });
    for (const key of NON_SECRET_KEYS) {
        const value = config.values[key]?.trim();
        if (value && process.env[key] === undefined)
            process.env[key] = value;
    }
    for (const key of CREDENTIAL_KEYS) {
        if (process.env[key]?.trim())
            continue;
        const resolved = resolveCredential(key, { config });
        if (resolved.value)
            process.env[key] = resolved.value;
        if (resolved.error) {
            ctx.logger.warn("agent-society-config: " +
                key +
                " credential unavailable (" +
                resolved.error +
                ")");
        }
    }
    // Host model settings and credentials remain owned by dsh. AgentSociety
    // credentials configure only this plugin's Hub and delegated operations.
    copy("AGENT_HUB_URL", "AGENT_SOCIETY_HUB_URL");
    copy("AGENT_WORKSPACE_ROOT", "AGENT_SOCIETY_WORKSPACE_ROOT");
    copy("AGENT_ACTOR_ID", "AGENT_SOCIETY_ACTOR_ID");
    copy("AGENT_NODE_ID", "AGENT_SOCIETY_NODE_ID");
    copy("AGENT_PRINCIPAL_ID", "AGENT_SOCIETY_PRINCIPAL_ID");
    copy("AGENT_PRINCIPAL_NAME", "AGENT_SOCIETY_DISPLAY_NAME");
    if (!process.env.AGENT_SOCIETY_MODEL) {
        copy("AGENT_DSH_MODEL", "AGENT_SOCIETY_MODEL");
        copy("AGENT_REMOTE_MODEL", "AGENT_SOCIETY_MODEL");
    }
    if (!process.env.AGENT_SOCIETY_PROVIDER) {
        copy("AGENT_DSH_PROVIDER", "AGENT_SOCIETY_PROVIDER");
    }
    if (!process.env.AGENT_SOCIETY_TOOL_POLICY) {
        const policy = process.env.AGENT_REMOTE_TOOL_POLICY;
        if (policy === "read_only")
            process.env.AGENT_SOCIETY_TOOL_POLICY = "read_only";
        else if (policy === "no_tools")
            process.env.AGENT_SOCIETY_TOOL_POLICY = "no_tools";
        else if (policy === "full")
            process.env.AGENT_SOCIETY_TOOL_POLICY = "full";
    }
    if (!process.env.AGENT_SOCIETY_HUB_TOKEN) {
        process.env.AGENT_SOCIETY_HUB_TOKEN =
            process.env.AGENT_HUB_NODE_TOKEN?.trim() ||
                process.env.AGENT_HUB_TOKEN?.trim() ||
                "";
    }
    if (!process.env.AGENT_SOCIETY_HUB_URL) {
        process.env.AGENT_SOCIETY_HUB_URL = "";
    }
}
function observeUsage(dshHome, next) {
    return (async function* () {
        let usage;
        try {
            for await (const chunk of next()) {
                if (chunk.type === "usage")
                    usage = chunk.usage;
                yield chunk;
            }
        }
        finally {
            if (usage !== undefined)
                recordAgentSocietyMetrics(dshHome, { usage });
        }
    })();
}
function copy(source, target) {
    if (process.env[target] === undefined && process.env[source]?.trim()) {
        process.env[target] = process.env[source].trim();
    }
}
//# sourceMappingURL=config-plugin.js.map