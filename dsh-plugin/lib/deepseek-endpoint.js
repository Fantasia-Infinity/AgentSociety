/** Translate legacy official Chat Completions roots to the native Messages root.
 * Custom gateways and explicit native adapter overrides are left unchanged.
 */
export function deepSeekMessagesEndpoint(value) {
    const root = value.trim().replace(/\/+$/u, "");
    if (root === "https://api.deepseek.com" || root === "https://api.deepseek.com/v1") {
        return "https://api.deepseek.com/anthropic";
    }
    return value;
}
//# sourceMappingURL=deepseek-endpoint.js.map