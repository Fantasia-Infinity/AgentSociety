#!/usr/bin/env node
/**
 * Minimal DeepSeek Harness SDK JSON-RPC runtime double used by
 * agent-host/test/dsh-engine.test.ts. It speaks the same newline-delimited
 * wire protocol as dsh-jsonrpc-agent but makes no model calls.
 */
import { createInterface } from "node:readline";

const pending = new Map();
const lines = createInterface({ input: process.stdin });
let nextMessage = 1;

function send(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

lines.on("line", (raw) => {
  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    send({
      jsonrpc: "2.0",
      id: null,
      error: { code: -32700, message: "invalid json" },
    });
    return;
  }

  if (message.method === "initialize") {
    send({
      jsonrpc: "2.0",
      id: message.id,
      result: {
        serverInfo: { name: "deepseek-harness-sdk-runtime", version: "0.0.1" },
      },
    });
    return;
  }

  if (message.method === "session/prompt") {
    const messageId = `msg-${nextMessage}`;
    nextMessage += 1;
    send({ jsonrpc: "2.0", id: message.id, result: { messageId } });
    const sessionId = message.params.sessionId;
    const text = message.params.contentBlocks
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("");

    if (process.env.FAKE_DSH_STALL === "1") {
      send({
        jsonrpc: "2.0",
        method: "session.status",
        params: { sessionId, status: "running" },
      });
      return;
    }

    const events = [
      {
        method: "session.status",
        params: { sessionId, status: "running" },
      },
      {
        method: "session.event",
        params: {
          sessionId,
          event: {
            type: "agent/inbox/spliced",
            seq: 0,
            time: Date.now(),
            data: {
              target: "next-turn",
              start: 0,
              inserted: [
                {
                  id: messageId,
                  role: "user",
                  content: [{ type: "text", text }],
                },
              ],
            },
          },
        },
      },
      {
        method: "session.event",
        params: {
          sessionId,
          event: {
            type: "turn/start",
            seq: 1,
            time: Date.now(),
            data: { turn: 1 },
          },
        },
      },
      {
        method: "session.event",
        params: {
          sessionId,
          event: {
            type: "user/message",
            seq: 2,
            time: Date.now(),
            data: {
              id: messageId,
              role: "user",
              content: [{ type: "text", text }],
            },
          },
        },
      },
      {
        method: "session.event",
        params: {
          sessionId,
          event: {
            type: "assistant/chunk",
            seq: 3,
            time: Date.now(),
            data: {
              turn: 1,
              step: 1,
              chunk: { type: "text-delta", index: 0, text: "MOCK_DSH_" },
            },
          },
        },
      },
      {
        method: "session.event",
        params: {
          sessionId,
          event: {
            type: "assistant/chunk",
            seq: 4,
            time: Date.now(),
            data: {
              turn: 1,
              step: 1,
              chunk: { type: "text-delta", index: 0, text: "ENGINE" },
            },
          },
        },
      },
      {
        method: "session.event",
        params: {
          sessionId,
          event: {
            type: "assistant/message",
            seq: 5,
            time: Date.now(),
            data: {
              turn: 1,
              step: 1,
              message: {
                role: "assistant",
                content: [{ type: "text", text: "MOCK_DSH_ENGINE" }],
              },
              usage: { inputTokens: 1, outputTokens: 1 },
            },
          },
        },
      },
      {
        method: "session.event",
        params: {
          sessionId,
          event: {
            type: "step/end",
            seq: 6,
            time: Date.now(),
            data: { turn: 1, step: 1 },
          },
        },
      },
      {
        method: "session.event",
        params: {
          sessionId,
          event: {
            type: "turn/end",
            seq: 7,
            time: Date.now(),
            data: { turn: 1, reason: { kind: "completed" } },
          },
        },
      },
      {
        method: "session.status",
        params: { sessionId, status: "idle" },
      },
    ];
    for (const notification of events) send(notification);
    return;
  }

  if (message.method === "shutdown") {
    send({ jsonrpc: "2.0", id: message.id, result: {} });
    setTimeout(() => process.exit(0), 10);
    return;
  }

  send({
    jsonrpc: "2.0",
    id: message.id,
    error: { code: -32601, message: "method not found" },
  });
});
