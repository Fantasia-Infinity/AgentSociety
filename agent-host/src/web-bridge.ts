/**
 * Device-side outbound DSH Web bridge.
 *
 * Opens an authenticated WebSocket tunnel to the Hub (so the device can sit
 * behind NAT/firewall), then forwards browser requests for the local `dsh web`
 * HTTP surface over that tunnel. The local dsh web stays bound to loopback.
 */

export interface WebBridgeOptions {
  hubUrl: string;
  nodeToken: string;
  nodeId: string;
  /** Local dsh web origin, e.g. http://127.0.0.1:3001. Must be loopback. */
  target: string;
  /** Log line callback, defaults to console.log. */
  log?: (message: string) => void;
}

const RECONNECT_DELAY_MS = 5_000;

export function assertLoopbackTarget(target: string): string {
  const url = new URL(target);
  const host = url.hostname.replace(/^\[|\]$/gu, "").toLowerCase();
  if (host !== "localhost" && host !== "::1" && !host.startsWith("127.")) {
    throw new Error(
      `web-bridge target must be loopback-only, got ${target}; ` +
        "the Hub must never reach non-loopback device surfaces",
    );
  }
  return target.replace(/\/$/u, "");
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export class WebBridge {
  private ws: WebSocket | undefined;
  private stopped = false;
  private readonly log: (message: string) => void;
  private readonly target: string;
  /** Hub-issued event stream id -> local device WebSocket. */
  private readonly eventStreams = new Map<string, WebSocket>();

  constructor(private readonly options: WebBridgeOptions) {
    this.log = options.log ?? console.log;
    this.target = assertLoopbackTarget(options.target);
  }

  /** Run until stop() is called, reconnecting with backoff on failure. */
  async run(): Promise<void> {
    while (!this.stopped) {
      try {
        await this.connectOnce();
      } catch (error) {
        if (this.stopped) break;
        this.log(
          `web-bridge connection failed: ${
            error instanceof Error ? error.message : String(error)
          }`,
        );
      }
      if (this.stopped) break;
      await delay(RECONNECT_DELAY_MS);
    }
  }

  stop(): void {
    this.stopped = true;
    for (const local of this.eventStreams.values()) {
      try {
        local.close();
      } catch {
        // already closing
      }
    }
    this.eventStreams.clear();
    this.ws?.close();
  }

  private async connectOnce(): Promise<void> {
    const ticket = await this.fetchTicket();
    const wsUrl = new URL(this.options.hubUrl);
    wsUrl.protocol = wsUrl.protocol === "https:" ? "wss:" : "ws:";
    wsUrl.pathname = "/v1/web/tunnel/ws";
    wsUrl.search = `?ticket=${encodeURIComponent(ticket)}`;
    const ws = new WebSocket(wsUrl.toString());
    this.ws = ws;
    await new Promise<void>((resolve, reject) => {
      ws.addEventListener("open", () => resolve(), { once: true });
      ws.addEventListener("error", () => {
        reject(new Error("tunnel WebSocket connect failed"));
      }, { once: true });
    });
    this.log(`web-bridge tunnel open as ${this.options.nodeId}`);
    ws.addEventListener("message", (event) => {
      void this.handleMessage(event.data).catch((error) => {
        this.log(
          `web-bridge message error: ${
            error instanceof Error ? error.message : String(error)
          }`,
        );
      });
    });
    await new Promise<void>((resolve) => {
      ws.addEventListener("close", () => resolve(), { once: true });
      ws.addEventListener("error", () => resolve(), { once: true });
    });
    for (const local of this.eventStreams.values()) {
      try {
        local.close();
      } catch {
        // already closing
      }
    }
    this.eventStreams.clear();
    this.log("web-bridge tunnel closed; reconnecting");
  }

  private async handleMessage(data: unknown): Promise<void> {
    const message = JSON.parse(String(data)) as Record<string, unknown>;
    if (message.type === "ping") {
      this.ws?.send(JSON.stringify({ type: "pong" }));
      return;
    }
    if (message.type === "ws-open") {
      this.openEventStream(message);
      return;
    }
    if (message.type === "ws-close") {
      const streamId = String(message.id ?? "");
      const local = this.eventStreams.get(streamId);
      if (local !== undefined) {
        this.eventStreams.delete(streamId);
        try {
          local.close();
        } catch {
          // already closing
        }
      }
      return;
    }
    if (message.type !== "http") return;
    const requestId = String(message.id ?? "");
    if (!requestId) return;
    const method = String(message.method ?? "GET").toUpperCase();
    const path = String(message.path ?? "/");
    const headers = (message.headers ?? {}) as Record<string, string>;
    const body = message.body_b64
      ? Buffer.from(String(message.body_b64), "base64")
      : undefined;
    let response: Response;
    try {
      response = await fetch(`${this.target}${path}`, {
        method,
        headers,
        ...(body !== undefined ? { body } : {}),
      });
    } catch (error) {
      this.ws?.send(
        JSON.stringify({
          type: "http-response",
          id: requestId,
          status: 502,
          headers: {},
          body_b64: Buffer.from(
            `device dsh web unreachable: ${
              error instanceof Error ? error.message : String(error)
            }`,
          ).toString("base64"),
        }),
      );
      return;
    }
    const responseBody = Buffer.from(await response.arrayBuffer());
    this.ws?.send(
      JSON.stringify({
        type: "http-response",
        id: requestId,
        status: response.status,
        headers: Object.fromEntries(response.headers.entries()),
        body_b64: responseBody.toString("base64"),
      }),
    );
  }

  /** Open one device-local DSH event downlink and relay its frames to the Hub. */
  private openEventStream(message: Record<string, unknown>): void {
    const streamId = String(message.id ?? "");
    if (!streamId) return;
    const path = String(message.path ?? "/");
    if (!/^\/api\/events\.(mux|host)$/u.test(path)) {
      this.ws?.send(
        JSON.stringify({
          type: "ws-open-ack",
          id: streamId,
          ok: false,
          error: "event path not allowed",
        }),
      );
      return;
    }
    let local: WebSocket;
    try {
      local = new WebSocket(`${this.target}${path}`);
    } catch (error) {
      this.ws?.send(
        JSON.stringify({
          type: "ws-open-ack",
          id: streamId,
          ok: false,
          error: error instanceof Error ? error.message : String(error),
        }),
      );
      return;
    }
    this.eventStreams.set(streamId, local);
    local.addEventListener("open", () => {
      this.ws?.send(
        JSON.stringify({ type: "ws-open-ack", id: streamId, ok: true }),
      );
    }, { once: true });
    local.addEventListener("message", (event) => {
      const frame = event.data;
      let opcode: number;
      let payload: Buffer;
      if (typeof frame === "string") {
        opcode = 1;
        payload = Buffer.from(frame);
      } else if (frame instanceof ArrayBuffer) {
        opcode = 2;
        payload = Buffer.from(frame);
      } else if (ArrayBuffer.isView(frame)) {
        opcode = 2;
        payload = Buffer.from(frame.buffer, frame.byteOffset, frame.byteLength);
      } else {
        return;
      }
      this.ws?.send(
        JSON.stringify({
          type: "ws-frame",
          id: streamId,
          opcode,
          payload_b64: payload.toString("base64"),
        }),
      );
    });
    local.addEventListener("error", () => {
      if (local.readyState === WebSocket.CONNECTING) {
        this.eventStreams.delete(streamId);
        this.ws?.send(
          JSON.stringify({
            type: "ws-open-ack",
            id: streamId,
            ok: false,
            error: "local event stream failed",
          }),
        );
      }
    }, { once: true });
    local.addEventListener("close", () => {
      if (this.eventStreams.delete(streamId)) {
        this.ws?.send(
          JSON.stringify({ type: "ws-close", id: streamId, code: 1000 }),
        );
      }
    }, { once: true });
  }

  private async fetchTicket(): Promise<string> {    const response = await fetch(
      `${this.options.hubUrl}/v1/hub/nodes/web/tunnel`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.options.nodeToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ node_id: this.options.nodeId }),
      },
    );
    if (!response.ok) {
      throw new Error(`tunnel ticket request failed: ${response.status}`);
    }
    const payload = (await response.json()) as { ticket?: string };
    if (!payload.ticket) {
      throw new Error("tunnel ticket response missing ticket");
    }
    return payload.ticket;
  }
}
