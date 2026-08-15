/**
 * Minimal AgentSociety Hub REST client used by the dsh worker plugin.
 * The Hub remains the durable coordination source; this package only speaks
 * the public REST contract.
 */
export class HubError extends Error {
    status;
    constructor(message, status) {
        super(message);
        this.status = status;
        this.name = "HubError";
    }
}
export class HubClient {
    baseUrl;
    token;
    fetchImpl;
    constructor(baseUrl, token, fetchImpl = fetch) {
        this.baseUrl = baseUrl;
        this.token = token;
        this.fetchImpl = fetchImpl;
    }
    async registerPrincipal(item) {
        await this.request("/v1/hub/principals", { method: "POST", body: item });
    }
    async registerActor(item) {
        await this.request("/v1/hub/actors", { method: "POST", body: item });
    }
    async registerNode(item) {
        await this.request("/v1/hub/nodes", { method: "POST", body: item });
    }
    async heartbeat(nodeId) {
        await this.request("/v1/hub/nodes/heartbeat", {
            method: "POST",
            body: { node_id: nodeId },
        });
    }
    async claimTask(item) {
        const response = await this.request("/v1/hub/tasks/claim", { method: "POST", body: item });
        return response.claim;
    }
    async updateTask(taskId, item) {
        await this.request(`/v1/hub/tasks/${encodeURIComponent(taskId)}/updates`, {
            method: "POST",
            body: item,
        });
    }
    async updateRun(runId, item) {
        await this.request(`/v1/hub/runs/${encodeURIComponent(runId)}/updates`, {
            method: "POST",
            body: item,
        });
    }
    async request(path, init) {
        const response = await this.fetchImpl(`${this.baseUrl.replace(/\/$/u, "")}${path}`, {
            method: init.method,
            headers: {
                Authorization: `Bearer ${this.token}`,
                ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
            },
            ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
        });
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new HubError(`Hub request ${path} failed (${response.status})${detail ? `: ${detail.slice(0, 500)}` : ""}`, response.status);
        }
        return (await response.json());
    }
}
//# sourceMappingURL=hub-client.js.map