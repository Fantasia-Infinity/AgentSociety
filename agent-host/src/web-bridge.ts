import { isIP } from "node:net";

/**
 * Device-side outbound DSH Web bridge.
 *
 * Opens an authenticated WebSocket tunnel to the Hub (so the device can sit
 * behind NAT/firewall), then forwards browser requests for the local `dsh web`
 * HTTP surface over that tunnel. The local dsh web stays bound to loopback.
 */

export type WebBridgeDelay = (
  milliseconds: number,
  signal?: AbortSignal,
) => Promise<void>;

export interface WebBridgeOptions {
  hubUrl: string;
  nodeToken: string;
  nodeId: string;
  /** Local dsh web origin, e.g. http://127.0.0.1:3080. Must be loopback. */
  target: string;
  /** Log line callback, defaults to console.log. */
  log?: (message: string) => void;
  /** Injectable WebSocket constructor, primarily for deterministic tests. */
  webSocketImpl?: typeof WebSocket;
  /** Injectable fetch implementation, primarily for deterministic tests. */
  fetchImpl?: typeof fetch;
  /** Injectable reconnect delay, primarily for deterministic tests. */
  delayImpl?: WebBridgeDelay;
}

const RECONNECT_DELAY_MS = 5_000;
/** Cap for device-side response bodies, matching the Hub's proxy limit. */
export const MAX_RESPONSE_BODY = 32 * 1024 * 1024;

export function assertLoopbackTarget(target: string): string {
  const url = new URL(target);
  const host = url.hostname.replace(/^\[|\]$/gu, "").toLowerCase();
  const isLoopback =
    host === "localhost" ||
    host === "::1" ||
    (isIP(host) === 4 && host.startsWith("127."));
  if (!isLoopback) {
    throw new Error(
      `web-bridge target must be loopback-only, got ${target}; ` +
        "the Hub must never reach non-loopback device surfaces",
    );
  }
  return target.replace(/\/$/u, "");
}

function delay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(done, milliseconds);
    const abort = () => done();
    function done(): void {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      resolve();
    }
    signal?.addEventListener("abort", abort, { once: true });
  });
}

/** Build a device-local request URL and enforce the path allowlist. */
export function buildLocalUrl(target: string, path: string): URL {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new Error("request path must be origin-absolute");
  }
  const fragmentIndex = path.indexOf("#");
  if (fragmentIndex >= 0) {
    throw new Error("request path must not contain a fragment");
  }
  const queryIndex = path.indexOf("?");
  const rawPath = queryIndex >= 0 ? path.slice(0, queryIndex) : path;
  if (
    rawPath.length === 0 ||
    rawPath.includes("\\") ||
    /[\u0000-\u001f\u007f]/u.test(rawPath)
  ) {
    throw new Error("request path contains invalid characters");
  }

  // WHATWG URL parsing normalizes dot segments before returning pathname. Do
  // the checks on the wire representation first, then compare the parser's
  // result below so a path cannot be authorized under one spelling and sent
  // to the local server under another.
  const rawSegments = rawPath.split("/");
  if (rawSegments.some((segment) => segment === "." || segment === "..")) {
    throw new Error("request path must not contain dot segments");
  }
  let decodedPath: string;
  try {
    decodedPath = decodeURIComponent(rawPath);
  } catch {
    throw new Error("request path contains invalid percent encoding");
  }
  if (
    decodedPath.includes("\\") ||
    decodedPath.split("/").some((segment) => segment === "." || segment === "..")
  ) {
    throw new Error("request path must not contain encoded dot segments");
  }
  let decodedAgain: string;
  try {
    decodedAgain = decodeURIComponent(decodedPath);
  } catch {
    throw new Error("request path contains invalid nested percent encoding");
  }
  if (
    decodedAgain.includes("\\") ||
    decodedAgain.split("/").some((segment) => segment === "." || segment === "..")
  ) {
    throw new Error("request path must not contain nested encoded dot segments");
  }

  const targetUrl = new URL(target);
  const url = new URL(path, targetUrl);
  const expectedSearch = queryIndex >= 0 ? path.slice(queryIndex) : "";
  if (
    url.origin !== targetUrl.origin ||
    url.pathname !== rawPath ||
    url.search !== expectedSearch
  ) {
    throw new Error("request path must stay on the device target origin");
  }
  const allowlist =
    rawPath === "/" ||
    rawPath === "/api" ||
    rawPath.startsWith("/api/") ||
    rawPath.startsWith("/assets/") ||
    rawPath.startsWith("/plugins/");
  if (!allowlist) {
    throw new Error("request path not allowed");
  }
  return url;
}

export class WebBridge {
  private ws: WebSocket | undefined;
  private stopped = false;
  private readonly abortController = new AbortController();
  private readonly log: (message: string) => void;
  private readonly target: string;
  private readonly WebSocketImpl: typeof WebSocket;
  private readonly fetchImpl: typeof fetch;
  private readonly delayImpl: WebBridgeDelay;
  /** Hub-issued event stream id -> local device WebSocket. */
  private readonly eventStreams = new Map<string, WebSocket>();
  /** Tunnel socket which owns each local event stream. */
  private readonly eventStreamOwners = new Map<string, WebSocket>();

  private closeEventStreams(owner?: WebSocket): void {
    for (const [streamId, local] of this.eventStreams) {
      if (owner !== undefined && this.eventStreamOwners.get(streamId) !== owner) {
        continue;
      }
      this.eventStreams.delete(streamId);
      this.eventStreamOwners.delete(streamId);
      try {
        local.close();
      } catch {
        // already closing
      }
    }
  }

  private sendTunnel(ws: WebSocket, payload: Record<string, unknown>): boolean {
    if (this.stopped || ws.readyState !== this.WebSocketImpl.OPEN) return false;
    try {
      ws.send(JSON.stringify(payload));
      return true;
    } catch {
      return false;
    }
  }

  constructor(private readonly options: WebBridgeOptions) {
    this.log = options.log ?? console.log;
    this.target = assertLoopbackTarget(options.target);
    this.WebSocketImpl = options.webSocketImpl ?? WebSocket;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.delayImpl = options.delayImpl ?? delay;
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
      await this.delayImpl(RECONNECT_DELAY_MS, this.abortController.signal);
    }
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.abortController.abort();
    this.closeEventStreams();
    const ws = this.ws;
    this.ws = undefined;
    if (ws !== undefined) {
      try {
        ws.close();
      } catch {
        // A socket can still be in CONNECTING when stop() is called.
      }
    }
  }

  private async connectOnce(): Promise<void> {
    const signal = this.abortController.signal;
    const ticket = await this.fetchTicket(signal);
    if (this.stopped) return;
    const wsUrl = new URL(this.options.hubUrl);
    wsUrl.protocol = wsUrl.protocol === "https:" ? "wss:" : "ws:";
    wsUrl.pathname = "/v1/web/tunnel/ws";
    wsUrl.search = `?ticket=${encodeURIComponent(ticket)}`;
    const ws = new this.WebSocketImpl(wsUrl.toString());
    this.ws = ws;
    let tornDown = false;
    const teardown = () => {
      if (tornDown) return;
      tornDown = true;
      if (this.ws !== ws) return;
      this.ws = undefined;
      this.closeEventStreams(ws);
    };
    ws.addEventListener("close", teardown, { once: true });
    ws.addEventListener("error", teardown, { once: true });
    ws.addEventListener("message", (event) => {
      void this.handleMessage(ws, event.data).catch((error) => {
        this.log(
          `web-bridge message error: ${
            error instanceof Error ? error.message : String(error)
          }`,
        );
      });
    });
    try {
      await this.waitForOpen(ws, signal);
      if (this.stopped) return;
      this.log(`web-bridge tunnel open as ${this.options.nodeId}`);
      await this.waitForClose(ws, signal);
      this.log("web-bridge tunnel closed; reconnecting");
    } finally {
      teardown();
      if (ws.readyState === this.WebSocketImpl.OPEN ||
          ws.readyState === this.WebSocketImpl.CONNECTING) {
        try {
          ws.close();
        } catch {
          // already closing or closed
        }
      }
    }
  }

  private waitForOpen(ws: WebSocket, signal: AbortSignal): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      if (signal.aborted) {
        reject(new Error("stopped"));
        return;
      }
      const onOpen = () => {
        cleanup();
        resolve();
      };
      const onError = () => {
        cleanup();
        reject(new Error("tunnel WebSocket connect failed"));
      };
      const onAbort = () => {
        cleanup();
        try {
          ws.close();
        } catch {
          // The socket may already be closed.
        }
        reject(new Error("stopped"));
      };
      function cleanup(): void {
        ws.removeEventListener("open", onOpen);
        ws.removeEventListener("error", onError);
        signal.removeEventListener("abort", onAbort);
      }
      ws.addEventListener("open", onOpen, { once: true });
      ws.addEventListener("error", onError, { once: true });
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  private waitForClose(ws: WebSocket, signal: AbortSignal): Promise<void> {
    return new Promise<void>((resolve) => {
      if (signal.aborted) {
        resolve();
        return;
      }
      const onClose = () => {
        cleanup();
        resolve();
      };
      const onError = () => {
        cleanup();
        resolve();
      };
      const onAbort = () => {
        cleanup();
        try {
          ws.close();
        } catch {
          // The socket may already be closed.
        }
        resolve();
      };
      function cleanup(): void {
        ws.removeEventListener("close", onClose);
        ws.removeEventListener("error", onError);
        signal.removeEventListener("abort", onAbort);
      }
      ws.addEventListener("close", onClose, { once: true });
      ws.addEventListener("error", onError, { once: true });
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  private async handleMessage(ws: WebSocket, data: unknown): Promise<void> {
    const message = JSON.parse(String(data)) as Record<string, unknown>;
    if (message.type === "ping") {
      this.sendTunnel(ws, { type: "pong" });
      return;
    }
    if (message.type === "ws-open") {
      this.openEventStream(ws, message);
      return;
    }
    if (message.type === "ws-close") {
      const streamId = String(message.id ?? "");
      const local = this.eventStreams.get(streamId);
      if (
        local !== undefined &&
        this.eventStreamOwners.get(streamId) === ws
      ) {
        this.eventStreams.delete(streamId);
        this.eventStreamOwners.delete(streamId);
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
      const url = buildLocalUrl(this.target, path);
      response = await this.fetchImpl(url, {
        method,
        headers,
        ...(body !== undefined ? { body } : {}),
        signal: this.abortController.signal,
      });
    } catch (error) {
      this.sendTunnel(ws, {
        type: "http-response",
        id: requestId,
        status: 502,
        headers: {},
        body_b64: Buffer.from(
          `device dsh web unreachable: ${
            error instanceof Error ? error.message : String(error)
          }`,
        ).toString("base64"),
      });
      return;
    }
    const responseBody = Buffer.from(await response.arrayBuffer());
    if (responseBody.byteLength > MAX_RESPONSE_BODY) {
      this.sendTunnel(ws, {
        type: "http-response",
        id: requestId,
        status: 502,
        headers: {},
        body_b64: Buffer.from("device response body too large").toString(
          "base64",
        ),
      });
      return;
    }
    this.sendTunnel(ws, {
      type: "http-response",
      id: requestId,
      status: response.status,
      headers: Object.fromEntries(response.headers.entries()),
      body_b64: responseBody.toString("base64"),
    });
  }

  /** Open one device-local DSH event downlink and relay its frames to the Hub. */
  private openEventStream(ws: WebSocket, message: Record<string, unknown>): void {
    const streamId = String(message.id ?? "");
    if (!streamId) return;
    const path = String(message.path ?? "/");
    if (!/^\/api\/events\.(mux|host)$/u.test(path)) {
      this.sendTunnel(ws, {
        type: "ws-open-ack",
        id: streamId,
        ok: false,
        error: "event path not allowed",
      });
      return;
    }
    let local: WebSocket;
    try {
      local = new this.WebSocketImpl(`${this.target}${path}`);
    } catch (error) {
      this.sendTunnel(ws, {
        type: "ws-open-ack",
        id: streamId,
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      });
      return;
    }
    const previous = this.eventStreams.get(streamId);
    if (previous !== undefined) {
      this.eventStreams.delete(streamId);
      this.eventStreamOwners.delete(streamId);
      try {
        previous.close();
      } catch {
        // already closing
      }
    }
    this.eventStreams.set(streamId, local);
    this.eventStreamOwners.set(streamId, ws);
    local.binaryType = "arraybuffer";
    let localOpened = false;
    local.addEventListener("open", () => {
      if (
        this.eventStreams.get(streamId) !== local ||
        this.eventStreamOwners.get(streamId) !== ws
      ) {
        return;
      }
      localOpened = true;
      this.sendTunnel(ws, { type: "ws-open-ack", id: streamId, ok: true });
    }, { once: true });
    local.addEventListener("message", (event) => {
      if (
        this.eventStreams.get(streamId) !== local ||
        this.eventStreamOwners.get(streamId) !== ws
      ) {
        return;
      }
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
      this.sendTunnel(ws, {
        type: "ws-frame",
        id: streamId,
        opcode,
        payload_b64: payload.toString("base64"),
      });
    });
    local.addEventListener("error", () => {
      if (
        this.eventStreams.get(streamId) !== local ||
        this.eventStreamOwners.get(streamId) !== ws
      ) {
        return;
      }
      this.eventStreams.delete(streamId);
      this.eventStreamOwners.delete(streamId);
      if (!localOpened) {
        this.sendTunnel(ws, {
          type: "ws-open-ack",
          id: streamId,
          ok: false,
          error: "local event stream failed",
        });
      } else {
        this.sendTunnel(ws, { type: "ws-close", id: streamId, code: 1011 });
      }
    }, { once: true });
    local.addEventListener("close", () => {
      if (
        this.eventStreams.get(streamId) !== local ||
        this.eventStreamOwners.get(streamId) !== ws
      ) {
        return;
      }
      this.eventStreams.delete(streamId);
      this.eventStreamOwners.delete(streamId);
      this.sendTunnel(ws, { type: "ws-close", id: streamId, code: 1000 });
    }, { once: true });
  }

  private async fetchTicket(signal: AbortSignal): Promise<string> {
    const response = await this.fetchImpl(
      `${this.options.hubUrl}/v1/hub/nodes/web/tunnel`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.options.nodeToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ node_id: this.options.nodeId }),
        signal,
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
