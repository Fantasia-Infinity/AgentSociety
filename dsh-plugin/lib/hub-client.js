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
    async getTask(taskId) {
        const response = await this.request(`/v1/hub/tasks/${encodeURIComponent(taskId)}`, { method: "GET" });
        return response.task;
    }
    async claimTaskControls(taskId, item) {
        const response = await this.request(`/v1/hub/tasks/${encodeURIComponent(taskId)}/controls/claim`, { method: "POST", body: item });
        return response.controls;
    }
    async acknowledgeTaskControl(taskId, controlId, item) {
        await this.request(`/v1/hub/tasks/${encodeURIComponent(taskId)}/controls/${encodeURIComponent(controlId)}/ack`, { method: "POST", body: item });
    }
    async updateTask(taskId, item) {
        const body = {
            run_id: item.run_id,
            lease_token: item.lease_token,
            status: item.status,
            message: item.message,
            result: item.result ?? {},
        };
        if (item.partial_result !== undefined) {
            body.partial_result = item.partial_result;
        }
        await this.request(`/v1/hub/tasks/${encodeURIComponent(taskId)}/updates`, {
            method: "POST",
            body,
        });
    }
    /**
     * Append one entry to the principal's shared memory (scope: consensus /
     * directory / qa). Idempotent when `event_id` is supplied: the Hub returns
     * the existing seq for a duplicate.
     */
    async appendSharedEvent(item) {
        const body = { ...item };
        const response = await this.request("/v1/hub/contexts/append", { method: "POST", body });
        return response.event;
    }
    /** Incremental pull of the shared memory log (after_seq = resume point). */
    async listSharedEvents(item = {}) {
        const query = new URLSearchParams();
        for (const [key, value] of Object.entries(item)) {
            if (value !== undefined)
                query.set(key, String(value));
        }
        const response = await this.request(`/v1/hub/contexts${query.size > 0 ? `?${query.toString()}` : ""}`, { method: "GET", body: undefined });
        return response.events;
    }
    /** Push one session directory row (identity enforced from the token). */
    async upsertDirectoryRow(item) {
        const response = await this.request(`/v1/hub/directory/${encodeURIComponent(item.session_id)}`, {
            method: "POST",
            body: {
                ...item.row,
                principal_id: item.principal_id,
                actor_id: item.actor_id,
                node_id: item.node_id,
            },
        });
        return response.row;
    }
    /** Incremental pull of the directory (latest row per session). */
    async listDirectory(item = {}) {
        const query = new URLSearchParams();
        for (const [key, value] of Object.entries(item)) {
            if (value !== undefined)
                query.set(key, String(value));
        }
        const response = await this.request(`/v1/hub/directory${query.size > 0 ? `?${query.toString()}` : ""}`, { method: "GET", body: undefined });
        return response.rows;
    }
    /** Drill into one session directory row (depth 0..3). */
    async getDirectoryRow(sessionId, depth = 0) {
        const response = await this.request(`/v1/hub/directory/${encodeURIComponent(sessionId)}?depth=${Math.min(Math.max(depth, 0), 3)}`, { method: "GET", body: undefined });
        return response.row;
    }
    /**
     * Subscribe to the worker push channel (`/v1/hub/events`) and invoke
     * `onEvent` for every SSE event. Resolves when the stream ends normally
     * (server closed the connection) and rejects on transport errors; the
     * caller owns reconnection with backoff.
     */
    async subscribeEvents(nodeId, onEvent, options = {}) {
        const url = `${this.baseUrl.replace(/\/$/u, "")}/v1/hub/events?node_id=${encodeURIComponent(nodeId)}`;
        const init = {
            headers: { Authorization: `Bearer ${this.token}` },
        };
        if (options.signal !== undefined)
            init.signal = options.signal;
        const response = await this.fetchImpl(url, init);
        if (!response.ok) {
            throw new HubError(`Hub events subscription failed (${response.status})`, response.status);
        }
        if (response.body === null)
            return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
            const { done, value } = await reader.read();
            if (done)
                return;
            buffer += decoder.decode(value, { stream: true });
            let boundary;
            while ((boundary = buffer.indexOf("\n\n")) >= 0) {
                const block = buffer.slice(0, boundary);
                buffer = buffer.slice(boundary + 2);
                let name = "message";
                let data = {};
                for (const line of block.split("\n")) {
                    if (line.startsWith("event: ")) {
                        name = line.slice(7).trim();
                    }
                    else if (line.startsWith("data: ")) {
                        try {
                            const parsed = JSON.parse(line.slice(6));
                            if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
                                data = parsed;
                            }
                        }
                        catch {
                            // Non-JSON data lines are ignored.
                        }
                    }
                }
                if (name !== "message" || Object.keys(data).length > 0) {
                    onEvent({ name, data });
                }
            }
        }
    }
    async addArtifact(item) {
        const response = await this.request("/v1/hub/artifacts", { method: "POST", body: item });
        return response.artifact;
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