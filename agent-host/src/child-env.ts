/**
 * Shared child-process environment policy: inherit the ambient environment,
 * scrub credentials owned by the AgentSociety process, and let the caller add
 * only the values the child runtime actually needs.
 */

const CREDENTIAL_ENV_PATTERN =
  /^(AGENT_HUB_(NODE_)?TOKEN|AGENT_HUB_PASSWORD|AGENT_HUB_API_TOKEN|AGENT_REMOTE_API_KEY|AGENT_API_KEY|LLM_API_KEY|AGENT_HUB_USERNAME|AGENT_HUB_SECRET|AGENT_HUB_(NODE_TOKEN|TOKEN|PASSWORD)_CREDENTIAL_SERVICE)$/u;

export function sanitizedChildEnv(
  env: NodeJS.ProcessEnv,
): NodeJS.ProcessEnv {
  const sanitized: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(env)) {
    if (CREDENTIAL_ENV_PATTERN.test(key)) continue;
    sanitized[key] = value;
  }
  return sanitized;
}

/** Backwards-compatible export for the generic Bridge worker. */
export const sanitizedAdapterEnv = sanitizedChildEnv;
